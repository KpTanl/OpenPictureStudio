use super::*;

impl State {
    fn partition_pass(&self, encoder: &mut wgpu::CommandEncoder, image: &GpuImage,
        state: &GpuImage, target: &GpuImage, bit: u32) -> PyResult<()> {
        let params = [image.width as u32, image.height as u32, image.stride as u32, image.offset as u32,
            state.width as u32, state.height as u32, state.stride as u32, state.offset as u32, bit, 0, 0, 0];
        let uniform = self.uniform("ops-partition-parameters", bytemuck::cast_slice(&params))?;
        self.dispatch(encoder, "beauty_partition", &[image.storage.buffer.as_ref().unwrap(),
            state.storage.buffer.as_ref().unwrap(), target.storage.buffer.as_ref().unwrap(), &uniform],
            (target.width as u32, target.height as u32));
        Ok(())
    }

    fn statistics_pass(&self, encoder: &mut wgpu::CommandEncoder, image: &GpuImage,
        field: &GpuImage, target: &GpuImage, mode: u32, args: &[f32]) -> PyResult<()> {
        let mut params = [0u32; 68];
        params[..4].copy_from_slice(&[target.width as u32, target.height as u32, mode, 0]);
        for (index, item) in [image, image, field, image].iter().enumerate() {
            params[4+index*4..8+index*4].copy_from_slice(&[item.width as u32, item.height as u32, item.stride as u32, item.offset as u32]);
        }
        for (index, value) in args.iter().enumerate() { params[20+index] = value.to_bits(); }
        let uniform = self.uniform("ops-statistics-parameters", bytemuck::cast_slice(&params))?;
        self.dispatch_named(encoder, "beauty_stats", &format!("beauty_stats.{mode}"), &[
            image.storage.buffer.as_ref().unwrap(), image.storage.buffer.as_ref().unwrap(),
            field.storage.buffer.as_ref().unwrap(), image.storage.buffer.as_ref().unwrap(),
            target.storage.buffer.as_ref().unwrap(), image.storage.buffer.as_ref().unwrap(), &uniform],
            ((target.width as u32).div_ceil(8), (target.height as u32).div_ceil(8)));
        Ok(())
    }
}

impl GpuDevice {
    pub(super) fn median_image(&self, py: Python<'_>, image: &GpuImage, cancelled: Option<Py<PyAny>>) -> PyResult<GpuImage> {
        self.state.image_check(image)?;
        let count = image.width * image.height;
        if count > 4_000_000 { return Err(error("beauty_target_limit", "Face statistics exceed GPU submission limits")); }
        py.allow_threads(|| {
            let mut io = self.state.io.lock().unwrap();
            self.state.encoder(&mut io)?;
            let ticket = self.state.ticket(&mut io);
            let allocate = |width, height| -> PyResult<GpuImage> {
                Ok(GpuImage { storage: self.state.allocate((width*height*16) as u64)?, width, height,
                    generation: self.state.generation, ticket: ticket.clone(), stride: width, offset: 0 })
            };
            let output = allocate(3, 1)?;
            if count <= 4096 {
                let params = [image.width as u32, image.height as u32, image.stride as u32, image.offset as u32];
                let uniform = self.state.uniform("ops-median-parameters", bytemuck::cast_slice(&params))?;
                self.state.dispatch(self.state.encoder(&mut io)?, "beauty_median", &[
                    image.storage.buffer.as_ref().unwrap(), output.storage.buffer.as_ref().unwrap(), &uniform], (3, 1));
                self.state.recorded(&mut io, 1, cancelled)?;
                return Ok(output);
            }
            let blocks = count.div_ceil(1024);
            let counts = allocate(blocks, 1)?;
            let total = allocate(1, 1)?;
            let states = [allocate(1, 3)?, allocate(1, 3)?];
            let partial = allocate(blocks, 3)?;
            let encoder = self.state.encoder(&mut io)?;
            self.state.statistics_pass(encoder, image, image, &counts, 12, &[])?;
            self.state.statistics_pass(encoder, &counts, &counts, &total, 15, &[])?;
            self.state.statistics_pass(encoder, &total, &total, &states[0], 13, &[])?;
            let mut current = 0;
            for bit in (0..32).rev() {
                self.state.partition_pass(encoder, image, &states[current], &partial, bit)?;
                self.state.statistics_pass(encoder, &partial, &states[current], &states[1-current], 11, &[bit as f32])?;
                current = 1-current;
            }
            self.state.statistics_pass(encoder, &states[current], &total, &output, 14, &[])?;
            self.state.recorded(&mut io, 68, cancelled)?;
            Ok(output)
        })
    }

    pub(super) fn components_image(&self, py: Python<'_>, image: &GpuImage, roots: &GpuImage,
                                   distance: f32, cancelled: Option<Py<PyAny>>) -> PyResult<GpuImage> {
        self.state.image_check(image)?;
        self.state.image_check(roots)?;
        let count = roots.width * roots.height;
        if count > 256 || image.width*image.height > 4_000_000 || !distance.is_finite() || distance <= 0.0 {
            return Err(error("beauty_target_limit", "Face component statistics exceed GPU submission limits"));
        }
        py.allow_threads(|| {
            let mut io = self.state.io.lock().unwrap();
            let target = self.state.allocate((count*64) as u64)?;
            let params = [image.width as u32, image.height as u32, image.stride as u32, image.offset as u32,
                roots.width as u32, roots.height as u32, roots.stride as u32, roots.offset as u32,
                distance.to_bits(), 0, 0, 0];
            let uniform = self.state.uniform("ops-components-parameters", bytemuck::cast_slice(&params))?;
            self.state.dispatch(self.state.encoder(&mut io)?, "beauty_components", &[
                image.storage.buffer.as_ref().unwrap(), roots.storage.buffer.as_ref().unwrap(),
                target.buffer.as_ref().unwrap(), &uniform], (count as u32, 1));
            let ticket = self.state.ticket(&mut io);
            self.state.recorded(&mut io, 1, cancelled)?;
            Ok(GpuImage { storage: target, width: 4, height: count, stride: 4, offset: 0,
                generation: self.state.generation, ticket })
        })
    }

    pub(super) fn nearest_visible_image(&self, py: Python<'_>, image: &GpuImage, cancelled: Option<Py<PyAny>>) -> PyResult<GpuImage> {
        self.state.image_check(image)?;
        py.allow_threads(|| {
            let mut io = self.state.io.lock().unwrap();
            let target = self.state.allocate((image.width * image.height * 16) as u64)?;
            let metadata = self.state.allocate(4)?;
            let mut params = [0u32;68];
            params[..4].copy_from_slice(&[image.width as u32,image.height as u32,16,0]);
            for index in 0..4 { params[4+index*4..8+index*4].copy_from_slice(&[image.width as u32,image.height as u32,image.stride as u32,image.offset as u32]); }
            let mut passes = 0;
            let mut submit = |io: &mut Io, params: &[u32], groups| -> PyResult<()> {
                let uniform = self.state.uniform("ops-nearest-visible", bytemuck::cast_slice(params))?;
                let input = image.storage.buffer.as_ref().unwrap();
                self.state.dispatch(self.state.encoder(io)?,"enhance",&[input,input,input,input,target.buffer.as_ref().unwrap(),metadata.buffer.as_ref().unwrap(),&uniform],groups);
                passes += 1;
                Ok(())
            };
            submit(&mut io,&params,((image.width as u32).div_ceil(8),(image.height as u32).div_ceil(8)))?;
            params[2] = 17;
            let count = image.width * image.height;
            for phase in 0..2 {
                for first in (0..count).step_by(65536) {
                    params[3] = first as u32; params[20] = ((count-first).min(65536) as f32).to_bits(); params[21] = (phase as f32).to_bits();
                    submit(&mut io,&params,(1,1))?;
                }
            }
            let ticket = self.state.ticket(&mut io);
            self.state.recorded(&mut io, passes, cancelled)?;
            Ok(GpuImage {storage:target,width:image.width,height:image.height,generation:self.state.generation,ticket,stride:image.width,offset:0})
        })
    }

    pub(super) fn assemble_images(&self, py: Python<'_>, images: Vec<PyRef<'_, GpuImage>>,
        positions: Vec<(usize, usize)>, width: usize, height: usize, cancelled: Option<Py<PyAny>>) -> PyResult<GpuImage> {
        if images.len() != positions.len() || images.len() > 4096 || width == 0 || height == 0 || width > 32768 || height > 32768 {
            return Err(error("target_limit", "Invalid image assembly"));
        }
        for (image, &(left, top)) in images.iter().zip(&positions) {
            self.state.image_check(image)?;
            if left + image.width > width || top + image.height > height { return Err(error("target_limit", "Image assembly exceeds output")); }
        }
        let parts: Vec<_> = images.iter().zip(positions).map(|(v, pos)|
            (v.storage.clone(), v.width, v.height, v.stride, v.offset, pos)).collect();
        py.allow_threads(|| {
            let mut io = self.state.io.lock().unwrap();
            let target = self.state.allocate((width * height * 16) as u64)?;
            let encoder = self.state.encoder(&mut io)?;
            let mut copies = 0;
            encoder.clear_buffer(target.buffer.as_ref().unwrap(), 0, None);
            for (storage, w, h, stride, offset, (left, top)) in &parts {
                for y in 0..*h {
                    encoder.copy_buffer_to_buffer(storage.buffer.as_ref().unwrap(), ((offset + y * stride) * 16) as u64,
                        target.buffer.as_ref().unwrap(), (((top + y) * width + left) * 16) as u64, (w * 16) as u64);
                    copies += 1;
                }
            }
            let ticket = self.state.ticket(&mut io);
            self.state.recorded(&mut io, 1 + copies / 64, cancelled)?;
            Ok(GpuImage { storage: target, width, height, generation: self.state.generation, ticket, stride: width, offset: 0 })
        })
    }

    pub(super) fn beauty_image(&self, py: Python<'_>, image: &GpuImage, auxiliary: &GpuImage,
        field: &GpuImage, extra: &GpuImage, kernel: &str, mode: u32, args: Vec<f32>, data: Vec<f32>,
        width: usize, height: usize, cancelled: Option<Py<PyAny>>) -> PyResult<GpuImage> {
        for item in [image, auxiliary, field, extra] { self.state.image_check(item)?; }
        if !["beauty", "beauty_stats", "beauty_spots", "enhance", "legacy"].contains(&kernel)
            || width == 0 || height == 0 || width > 32768 || height > 32768
            || args.len() > 48 || data.len() > if kernel == "legacy" { 4_000_000 } else { 8192 }
            || args.iter().chain(&data).any(|v| !v.is_finite()) {
            return Err(error("beauty_specification", "Invalid beauty operation"));
        }
        py.allow_threads(|| {
            let mut io = self.state.io.lock().unwrap();
            let target = self.state.allocate((width * height * 16) as u64)?;
            let mut params = [0u32; 68];
            params[..4].copy_from_slice(&[width as u32, height as u32, mode, 0]);
            for (index, item) in [image, auxiliary, field, extra].iter().enumerate() {
                params[4 + index * 4..8 + index * 4].copy_from_slice(&[item.width as u32, item.height as u32, item.stride as u32, item.offset as u32]);
            }
            for (index, value) in args.iter().enumerate() { params[20 + index] = value.to_bits(); }
            let uniform = self.state.uniform("ops-beauty-parameters", bytemuck::cast_slice(&params))?;
            let metadata = self.state.data("ops-beauty-metadata", &data)?;
            self.state.dispatch_named(self.state.encoder(&mut io)?, kernel, &format!("{kernel}.{mode}"), &[image.storage.buffer.as_ref().unwrap(),
                auxiliary.storage.buffer.as_ref().unwrap(), field.storage.buffer.as_ref().unwrap(),
                extra.storage.buffer.as_ref().unwrap(), target.buffer.as_ref().unwrap(), &metadata, &uniform],
                ((width as u32).div_ceil(8), (height as u32).div_ceil(8)));
            let ticket = self.state.ticket(&mut io);
            self.state.recorded(&mut io, 1, cancelled)?;
            Ok(GpuImage { storage: target, width, height, generation: self.state.generation, ticket, stride: width, offset: 0 })
        })
    }
}
