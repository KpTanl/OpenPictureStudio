use numpy::{PyArray1, PyArray2, PyArrayMethods, PyReadonlyArray1, PyReadonlyArray2, PyUntypedArrayMethods};
use pyo3::prelude::*;
use rayon::prelude::*;
use crate::{cancellation::Cancellation, thread_pools};

pub(crate) fn validate_blocks(data: &[u8], blocks: &[u32], expected: usize) -> PyResult<usize> {
    if blocks.is_empty() || blocks.len() % 4 != 0 || blocks.len() / 4 > 16384 || expected == 0 || expected > 1 << 30 {
        return Err(pyo3::exceptions::PyValueError::new_err("Invalid source block index"));
    }
    let block_size = blocks[3] as usize;
    let (mut input, mut output) = (0usize, 0usize);
    for (index, block) in blocks.chunks_exact(4).enumerate() {
        let (offset, length, start, count) = (block[0] as usize, block[1] as usize, block[2] as usize, block[3] as usize);
        if offset != input || start != output || length == 0 || count == 0 || count > block_size
            || block_size > 1 << 20 || offset + length > data.len()
            || index + 1 < blocks.len() / 4 && count != block_size {
            return Err(pyo3::exceptions::PyValueError::new_err("Invalid source block extent"));
        }
        input += length.div_ceil(4) * 4;
        output += count;
    }
    if input != data.len() || output != expected {
        return Err(pyo3::exceptions::PyValueError::new_err("Source block length mismatch"));
    }
    Ok(block_size)
}

#[pyfunction]
#[pyo3(signature = (data, block_size=262144, threads=None, cancelled=None))]
pub fn source_compress_blocks<'py>(py: Python<'py>, data: PyReadonlyArray1<'_, u8>, block_size: usize,
    threads: Option<usize>, cancelled: Option<Py<PyAny>>) -> PyResult<(Bound<'py, PyArray1<u8>>, Bound<'py, PyArray2<u32>>)> {
    let source = data.as_slice()?;
    if source.is_empty() || source.len() > 1 << 30 || !(1..=1 << 20).contains(&block_size)
        || source.len().div_ceil(block_size) > 16384 {
        return Err(pyo3::exceptions::PyValueError::new_err("Invalid source compression size"));
    }
    let parts = py.allow_threads(|| thread_pools::install(threads, || {
        source.par_chunks(block_size).map(|part| {
            let abort = cancelled.as_ref().map(|value| Python::with_gil(|py| value.clone_ref(py)));
            Cancellation::new(abort).check()?;
            Ok(lz4_flex::block::compress(part))
        }).collect::<PyResult<Vec<_>>>()
    }))??;
    let mut packed = Vec::new();
    let mut index = Vec::with_capacity(parts.len() * 4);
    for (number, part) in parts.iter().enumerate() {
        let start = number * block_size;
        index.extend_from_slice(&[packed.len() as u32, part.len() as u32, start as u32,
            (source.len() - start).min(block_size) as u32]);
        packed.extend_from_slice(part);
        packed.resize(packed.len().next_multiple_of(4), 0);
    }
    let count = parts.len();
    Ok((PyArray1::from_vec_bound(py, packed), PyArray1::from_vec_bound(py, index).reshape([count, 4])?))
}

#[pyfunction]
#[pyo3(signature = (data, blocks, expected, threads=None, cancelled=None))]
pub fn source_decompress_blocks<'py>(py: Python<'py>, data: PyReadonlyArray1<'_, u8>, blocks: PyReadonlyArray2<'_, u32>,
    expected: usize, threads: Option<usize>, cancelled: Option<Py<PyAny>>) -> PyResult<Bound<'py, PyArray1<u8>>> {
    if blocks.shape()[1] != 4 { return Err(pyo3::exceptions::PyValueError::new_err("Expected Nx4 source index")); }
    let (input, index) = (data.as_slice()?, blocks.as_slice()?);
    let size = validate_blocks(input, index, expected)?;
    let mut output = vec![0u8; expected];
    py.allow_threads(|| thread_pools::install(threads, || {
        output.par_chunks_mut(size).zip(index.par_chunks(4)).try_for_each(|(target, block)| {
            let abort = cancelled.as_ref().map(|value| Python::with_gil(|py| value.clone_ref(py)));
            Cancellation::new(abort).check()?;
            let offset = block[0] as usize;
            let count = lz4_flex::block::decompress_into(&input[offset..offset + block[1] as usize], target)
                .map_err(|error| pyo3::exceptions::PyValueError::new_err(error.to_string()))?;
            if count != target.len() { return Err(pyo3::exceptions::PyValueError::new_err("Source block output mismatch")); }
            Ok(())
        })
    }))??;
    Ok(PyArray1::from_vec_bound(py, output))
}

#[pyfunction]
#[pyo3(signature = (data, width, height, step, cancelled=None))]
pub fn source_unfilter_png<'py>(py: Python<'py>, data: PyReadonlyArray1<'_, u8>, width: usize, height: usize,
    step: usize, cancelled: Option<Py<PyAny>>) -> PyResult<Bound<'py, PyArray1<u8>>> {
    let row = width.checked_mul(step).ok_or_else(|| pyo3::exceptions::PyValueError::new_err("PNG size overflow"))?;
    let length = row.checked_add(1).and_then(|scan| scan.checked_mul(height));
    if width == 0 || height == 0 || !(1..=8).contains(&step) || length != Some(data.len()) {
        return Err(pyo3::exceptions::PyValueError::new_err("Invalid PNG scanline size"));
    }
    let input = data.as_slice()?;
    let mut output = vec![0u8; row * height];
    py.allow_threads(|| {
        let mut abort = Cancellation::new(cancelled);
        for y in 0..height {
            abort.check()?;
            let filter = input[y * (row + 1)];
            if filter > 4 { return Err(pyo3::exceptions::PyValueError::new_err("Invalid PNG filter")); }
            for x in 0..row {
                let left = if x >= step { output[y * row + x - step] } else { 0 };
                let up = if y != 0 { output[(y - 1) * row + x] } else { 0 };
                let corner = if x >= step && y != 0 { output[(y - 1) * row + x - step] } else { 0 };
                let predictor = match filter {
                    1 => left, 2 => up, 3 => ((left as u16 + up as u16) / 2) as u8,
                    4 => { let a = (up as i32 - corner as i32).abs(); let b = (left as i32 - corner as i32).abs();
                        let c = (left as i32 + up as i32 - 2 * corner as i32).abs();
                        if a <= b && a <= c { left } else if b <= c { up } else { corner } },
                    _ => 0,
                };
                output[y * row + x] = input[y * (row + 1) + x + 1].wrapping_add(predictor);
            }
        }
        Ok(())
    })?;
    Ok(PyArray1::from_vec_bound(py, output))
}
