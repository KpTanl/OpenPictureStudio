use super::*;
use numpy::PyReadonlyArray1;

impl GpuDevice {
    pub(super) fn bytes_image(&self, py: Python<'_>, data: PyReadonlyArray1<'_, u8>, width: usize, height: usize,
                              channels: usize, depth: usize, filtered: bool, cancelled: Option<Py<PyAny>>,
                              blocks: Option<PyReadonlyArray2<'_, u32>>) -> PyResult<GpuImage> {
        let row = width * channels * (depth / 8) + filtered as usize;
        if width == 0 || height == 0 || width.max(height) > 32768 || !(1..=4).contains(&channels) || !matches!(depth, 8 | 16)
            || blocks.is_none() && data.len() != row * height || blocks.as_ref().is_some_and(|index| index.shape()[1] != 4) {
            return Err(error("target_limit", "Invalid integer source specification"));
        }
        if filtered && blocks.is_none() && data.as_slice()?.iter().step_by(row).any(|value| *value > 4) {
            return Err(error("source_format", "Invalid PNG filter"));
        }
        let index = blocks.as_ref().map(|index| index.as_slice()).transpose()?;
        let bytes = data.as_slice()?;
        py.allow_threads(|| {
            let mut io = self.state.io.lock().unwrap();
            let clone_cancel = || cancelled.as_ref().map(|value| Python::with_gil(|py| value.clone_ref(py)));
            let storage = self.state.source_bytes(&mut io, bytes, width, height, channels, depth, filtered, index, clone_cancel())?;
            let event = self.state.flush(&mut io, clone_cancel())?;
            Ok(GpuImage { storage, width: row, height, generation: self.state.generation, ticket: done(event), stride: row, offset: 0 })
        })
    }

    pub(super) fn rows_image(&self, py: Python<'_>, source: &GpuImage, width: usize, height: usize, channels: usize,
                             depth: usize, filtered: bool, big_endian: bool, lut: PyReadonlyArray1<'_, f32>,
                             top: usize, rows: usize, matrix: Option<Vec<f32>>, cancelled: Option<Py<PyAny>>) -> PyResult<GpuImage> {
        self.state.image_check(source)?;
        let matrix = matrix.unwrap_or_default();
        let row = width * channels * (depth / 8) + filtered as usize;
        if !(1..=4).contains(&channels) || !matches!(depth, 8 | 16) || source.width != row || source.height != height
            || rows == 0 || top + rows > height || lut.len() != 1 << depth || lut.as_slice()?.iter().any(|v| !v.is_finite())
            || (!matrix.is_empty() && (matrix.len() != 9 || channels != 3 || depth != 16 || matrix.iter().any(|v| !v.is_finite()))) {
            return Err(error("target_limit", "Invalid integer source rows"));
        }
        let transfer = lut.as_slice()?;
        py.allow_threads(|| {
            let mut io = self.state.io.lock().unwrap();
            let image = self.state.source_rows(&mut io, &source.storage, width, height, channels, depth, filtered, big_endian,
                                               transfer, width, top, rows, false, &matrix)?;
            self.state.recorded(&mut io, 1, cancelled)?;
            Ok(image)
        })
    }

    pub(super) fn display_buffer_image(&self, width: usize, height: usize) -> PyResult<GpuImage> {
        if width == 0 || height == 0 || width.max(height) > 32768 {
            return Err(error("target_limit", "Invalid display target"));
        }
        let storage = self.state.allocate((width * height * 4) as u64)?;
        Ok(GpuImage { storage, width, height, generation: self.state.generation,
                      ticket: done(self.state.completed.load(Ordering::Acquire)), stride: width, offset: 0 })
    }

    pub(super) fn display_rows(&self, py: Python<'_>, target: &GpuImage, image: &GpuImage, width: usize, height: usize,
                               region: (usize, usize, usize, usize), rows: (usize, usize), origin: usize,
                               full: (usize, usize), cancelled: Option<Py<PyAny>>) -> PyResult<()> {
        self.state.image_check(target)?;
        self.state.image_check(image)?;
        let (left, top, right, bottom) = region;
        let (first, last) = rows;
        let (full_width, full_height) = full;
        if width == 0 || height == 0 || width > full_width || height > full_height || image.width != full_width
            || left >= right || top >= bottom || right > width || bottom > height
            || target.width != right - left || target.height != bottom - top || target.storage.size < ((right - left) * (bottom - top) * 4) as u64
            || first < top || first >= last || last > bottom || origin + image.height > full_height {
            return Err(error("target_limit", "GPU display rows are incompatible"));
        }
        if (full_width.div_ceil(width) + 1) * (full_height.div_ceil(height) + 1) > 4096 {
            return Err(error("target_limit", "GPU display footprint exceeds submission limit"));
        }
        let needed = if (width, height) == full { (first, last) } else {
            let scale = full_height as f32 / height as f32;
            ((first as f32 * scale).floor() as usize, ((last as f32 * scale).min(full_height as f32).ceil() as usize).min(full_height))
        };
        if needed.0 < origin || needed.1 > origin + image.height {
            return Err(error("target_limit", "GPU display rows exceed the computed rows"));
        }
        py.allow_threads(|| {
            let mut io = self.state.io.lock().unwrap();
            self.state.check()?;
            let uniform = self.state.uniform("ops-display-rows", bytemuck::cast_slice(&[full_width as u32, full_height as u32,
                width as u32, height as u32, image.stride as u32, image.offset as u32, left as u32, first as u32,
                (right - left) as u32, (last - first) as u32, origin as u32, (first - top) as u32]))?;
            self.state.dispatch(self.state.encoder(&mut io)?, "display_rgba", &[image.storage.buffer.as_ref().unwrap(),
                target.storage.buffer.as_ref().unwrap(), &uniform],
                (((right - left) as u32).div_ceil(8), ((last - first) as u32).div_ceil(8)));
            self.state.recorded(&mut io, 1, cancelled)
        })
    }

    pub(super) fn read_display_image<'py>(&self, py: Python<'py>, target: &GpuImage,
                                          cancelled: Option<Py<PyAny>>) -> PyResult<Bound<'py, PyArray3<u8>>> {
        self.state.image_check(target)?;
        let size = (target.width * target.height * 4) as u64;
        let output = py.allow_threads(|| {
            let mut io = self.state.io.lock().unwrap();
            let staging = self.state.read(&mut io, target.storage.buffer.as_ref().unwrap(), size)?;
            self.state.mapped(&mut io, &staging, size, cancelled)
        })?;
        PyArray1::from_vec_bound(py, output).reshape([target.height, target.width, 4])
    }
}
