use super::*;

impl GpuDevice {
    pub(super) fn color_image(&self, py: Python<'_>, image: &GpuImage, auxiliary: &GpuImage,
        field: &GpuImage, extra: &GpuImage, mode: u32, args: Vec<f32>, data: Vec<f32>,
        cancelled: Option<Py<PyAny>>) -> PyResult<GpuImage> {
        for item in [image, auxiliary, field, extra] { self.state.image_check(item)?; }
        if (mode > 21 && !(32..=41).contains(&mode)) || args.len() > 32 || data.len() > 1_048_576
            || args.iter().chain(&data).any(|v| !v.is_finite()) {
            return Err(error("color_specification", "Invalid GPU color specification"));
        }
        py.allow_threads(|| {
            let mut io = self.state.io.lock().unwrap();
            let target = self.state.allocate((image.width * image.height * 16) as u64)?;
            let mut params = [0u32; 52];
            params[..4].copy_from_slice(&[image.width as u32, image.height as u32, mode, 0]);
            for (index, item) in [image, auxiliary, field, extra].iter().enumerate() {
                params[4+index*4..8+index*4].copy_from_slice(&[item.width as u32, item.height as u32,
                    item.stride as u32, item.offset as u32]);
            }
            for (index, value) in args.iter().enumerate() { params[index+20] = value.to_bits(); }
            let uniform = self.state.uniform("ops-color-parameters", bytemuck::cast_slice(&params))?;
            let metadata = self.state.data("ops-color-metadata", &data)?;
            self.state.dispatch(self.state.encoder(&mut io)?, if mode>=32 { "face_regions" } else { "color" }, &[image.storage.buffer.as_ref().unwrap(),
                auxiliary.storage.buffer.as_ref().unwrap(), field.storage.buffer.as_ref().unwrap(),
                extra.storage.buffer.as_ref().unwrap(), target.buffer.as_ref().unwrap(),
                &metadata, &uniform],
                ((image.width as u32).div_ceil(8), (image.height as u32).div_ceil(8)));
            let ticket = self.state.ticket(&mut io);
            self.state.recorded(&mut io, 1, cancelled)?;
            Ok(GpuImage { storage: target, width: image.width, height: image.height,
                generation: self.state.generation, ticket, stride: image.width, offset: 0 })
        })
    }
}
