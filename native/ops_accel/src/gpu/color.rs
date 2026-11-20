use super::*;

impl GpuDevice {
    pub(super) fn color_image(&self, py: Python<'_>, image: &GpuImage, auxiliary: &GpuImage,
        field: &GpuImage, extra: &GpuImage, mode: u32, args: Vec<f32>, data: Vec<f32>,
        frame: Option<(usize, usize, usize, usize)>, into: Option<(&GpuImage, usize)>,
        cancelled: Option<Py<PyAny>>) -> PyResult<GpuImage> {
        for item in [image, auxiliary, field, extra] { self.state.image_check(item)?; }
        let frame = frame.unwrap_or((image.width, image.height, 0, 0));
        if (mode > 22 && !(32..=41).contains(&mode)) || args.len() > 32 || data.len() > 1_048_576
            || args.iter().chain(&data).any(|v| !v.is_finite())
            || frame.2 + image.width > frame.0 || frame.3 + image.height > frame.1 || frame.0.max(frame.1) > 32768 {
            return Err(error("color_specification", "Invalid GPU color specification"));
        }
        if let Some((target, row)) = into {
            self.state.image_check(target)?;
            if target.width != image.width || target.stride != target.width || target.offset != 0
                || row + image.height > target.height
                || [image, auxiliary, field, extra].iter().any(|item| Arc::ptr_eq(&item.storage, &target.storage)) {
                return Err(error("color_specification", "Invalid GPU color target rows"));
            }
        }
        py.allow_threads(|| {
            let mut io = self.state.io.lock().unwrap();
            let (target, row) = match into {
                Some((target, row)) => (target.storage.clone(), row),
                None => (self.state.allocate((image.width * image.height * 16) as u64)?, 0),
            };
            let mut params = [0u32; 56];
            params[..4].copy_from_slice(&[image.width as u32, image.height as u32, mode, row as u32]);
            for (index, item) in [image, auxiliary, field, extra].iter().enumerate() {
                params[4+index*4..8+index*4].copy_from_slice(&[item.width as u32, item.height as u32,
                    item.stride as u32, item.offset as u32]);
            }
            for (index, value) in args.iter().enumerate() { params[index+20] = value.to_bits(); }
            params[52..].copy_from_slice(&[frame.0 as u32, frame.1 as u32, frame.2 as u32, frame.3 as u32]);
            let uniform = self.state.uniform("ops-color-parameters", bytemuck::cast_slice(&params))?;
            let metadata = self.state.data("ops-color-metadata", &data)?;
            self.state.dispatch(self.state.encoder(&mut io)?, if mode>=32 { "face_regions" } else { "color" }, &[image.storage.buffer.as_ref().unwrap(),
                auxiliary.storage.buffer.as_ref().unwrap(), field.storage.buffer.as_ref().unwrap(),
                extra.storage.buffer.as_ref().unwrap(), target.buffer.as_ref().unwrap(),
                &metadata, &uniform],
                ((image.width as u32).div_ceil(8), (image.height as u32).div_ceil(8)));
            let ticket = self.state.ticket(&mut io);
            self.state.recorded(&mut io, 1, cancelled)?;
            let height = into.map_or(image.height, |(target, _)| target.height);
            Ok(GpuImage { storage: target, width: image.width, height,
                generation: self.state.generation, ticket, stride: image.width, offset: 0 })
        })
    }
}
