use super::*;
use numpy::PyReadonlyArray1;

impl State {
    fn source_image(self: &Arc<Self>, io: &mut Io, data: &[u8], width: usize, height: usize, channels: usize,
                    depth: usize, filtered: bool, big_endian: bool, lut: &[f32], output_width: usize,
                    output_height: usize, matrix: &[f32], blocks: Option<&[u32]>, cancelled: Option<Py<PyAny>>) -> PyResult<GpuImage> {
        self.check()?;
        if !self.pipelines.contains_key("source_input") || filtered && !self.pipelines.contains_key("png_restore") {
            return Err(error("source_input_capability", "GPU source input kernel is unavailable"));
        }
        let clone_cancel = || cancelled.as_ref().map(|value| Python::with_gil(|py| value.clone_ref(py)));
        self.flush(io, clone_cancel())?;
        let bytes_per_pixel = channels * (depth / 8);
        let size = data.len().div_ceil(4) * 4;
        let input = if let Some(index) = blocks {
            self.source_blocks(io, data, index, (width * bytes_per_pixel + filtered as usize) * height, clone_cancel())?
        } else {
            let input = self.allocate(size as u64)?;
            {
                let mut pool = self.pool.lock().unwrap();
                if pool.total() + size as u64 > pool.limit { pool.idle.clear(); }
                if pool.total() + size as u64 > pool.limit { return Err(error("memory_pressure", "Integer upload staging exceeds budget")); }
                pool.transient = size as u64;
            }
            let upload = (|| {
                let whole = data.len() / 4 * 4;
                if whole != 0 { self.queue.write_buffer(input.buffer.as_ref().unwrap(), 0, &data[..whole]); }
                if whole < data.len() {
                    let mut tail = [0u8; 4];
                    tail[..data.len() - whole].copy_from_slice(&data[whole..]);
                    self.queue.write_buffer(input.buffer.as_ref().unwrap(), whole as u64, &tail);
                }
                self.wait(self.queue.submit([]), clone_cancel())
            })();
            self.pool.lock().unwrap().transient = 0;
            upload?;
            self.uploaded.fetch_add(size as u64, Ordering::Relaxed);
            input
        };
        if filtered {
            let status = self.allocate(4)?;
            self.queue.write_buffer(status.buffer.as_ref().unwrap(), 0, &[0u8; 4]);
            let tiles_x = width.div_ceil(64);
            let tiles_y = height.div_ceil(16);
            for begin in (0..tiles_x + tiles_y - 1).step_by(16) {
                let encoder = self.encoder(io)?;
                for diagonal in begin..(begin + 16).min(tiles_x + tiles_y - 1) {
                    let first = diagonal.saturating_sub(tiles_y - 1);
                    let last = diagonal.min(tiles_x - 1);
                    let params = [width as u32, height as u32, bytes_per_pixel as u32, 0,
                                  diagonal as u32, first as u32, 0, 0];
                    let uniform = self.uniform("ops-png-wave", bytemuck::cast_slice(&params))?;
                    self.dispatch(encoder, "png_restore", &[input.buffer.as_ref().unwrap(), &uniform,
                        status.buffer.as_ref().unwrap()],
                        ((last - first + 1) as u32, 1));
                }
                self.flush(io, clone_cancel())?;
            }
            if blocks.is_some() {
                let staging = self.read(io, status.buffer.as_ref().unwrap(), 4)?;
                let values = self.mapped(io, &staging, 4, clone_cancel())?;
                if values.iter().any(|value| *value != 0) {
                    return Err(error("source_blocks", "Invalid PNG filter in compressed source"));
                }
            }
        }
        let table = self.allocate((lut.len() * 4) as u64)?;
        self.queue.write_buffer(table.buffer.as_ref().unwrap(), 0, bytemuck::cast_slice(lut));
        self.uploaded.fetch_add((lut.len() * 4) as u64, Ordering::Relaxed);
        let target = self.allocate((output_width * height * 16) as u64)?;
        let resize = output_width != width || output_height != height;
        let mut params = [0u32; 24];
        params[..12].copy_from_slice(&[width as u32, height as u32, channels as u32, (depth / 8) as u32,
            output_width as u32, height as u32, filtered as u32, big_endian as u32,
            resize as u32, (!matrix.is_empty()) as u32, 0, 0]);
        for (i, value) in matrix.iter().enumerate() { params[12 + i / 3 * 4 + i % 3] = value.to_bits(); }
        let uniform = self.uniform("ops-source-input", bytemuck::cast_slice(&params))?;
        self.dispatch(self.encoder(io)?, "source_input", &[input.buffer.as_ref().unwrap(), table.buffer.as_ref().unwrap(),
            target.buffer.as_ref().unwrap(), &uniform], ((output_width as u32).div_ceil(8), (height as u32).div_ceil(8)));
        let ticket = self.ticket(io);
        let mut image = GpuImage { storage: target, width: output_width, height, generation: self.generation,
                                   ticket, stride: output_width, offset: 0 };
        if output_height != height {
            image = self.encode_image_op(io, &image, &image, 9, &[0.0, height as f32], output_width, output_height)?;
        }
        if resize && matches!(channels, 2 | 4) {
            image = self.encode_image_op(io, &image, &image, 3, &[], output_width, output_height)?;
        }
        self.flush(io, clone_cancel())?;
        drop(input);
        drop(table);
        Ok(image)
    }
}

impl GpuDevice {
    pub(super) fn prepare_source_image(&self, py: Python<'_>, data: PyReadonlyArray1<'_, u8>, width: usize, height: usize,
                    channels: usize, depth: usize, filtered: bool, big_endian: bool,
                    lut: PyReadonlyArray1<'_, f32>, output_width: usize, output_height: usize,
                    matrix: Option<Vec<f32>>, cancelled: Option<Py<PyAny>>, blocks: Option<PyReadonlyArray2<'_, u32>>) -> PyResult<GpuImage> {
        let matrix = matrix.unwrap_or_default();
        if width == 0 || height == 0 || width.max(height) > 32768 || width * height > 100_000_000
            || output_width == 0 || output_height == 0 || output_width > width || output_height > height
            || !(1..=4).contains(&channels) || !matches!(depth, 8 | 16)
            || blocks.is_none() && data.len() != (width * channels * (depth / 8) + filtered as usize) * height
            || (!matrix.is_empty() && (matrix.len() != 9 || channels != 3 || depth != 16 || matrix.iter().any(|v| !v.is_finite())))
            || lut.len() != 1 << depth || lut.as_slice()?.iter().any(|v| !v.is_finite())
            || 6.0 * (width as f32 / output_width as f32).max(height as f32 / output_height as f32) > 4096.0 {
            return Err(error("target_limit", "Invalid integer source specification"));
        }
        if filtered && blocks.is_none() {
            let stride = width * channels * (depth / 8) + 1;
            if data.as_slice()?.iter().step_by(stride).any(|value| *value > 4) {
                return Err(error("source_format", "Invalid PNG filter"));
            }
        }
        if blocks.as_ref().is_some_and(|index| index.shape()[1] != 4) {
            return Err(error("source_blocks", "Expected Nx4 compressed source index"));
        }
        let index = blocks.as_ref().map(|index| index.as_slice()).transpose()?;
        let bytes = data.as_slice()?;
        let transfer = lut.as_slice()?;
        py.allow_threads(|| self.state.source_image(&mut self.state.io.lock().unwrap(), bytes, width, height, channels, depth, filtered, big_endian,
            transfer, output_width, output_height, &matrix, index, cancelled))
    }
}
