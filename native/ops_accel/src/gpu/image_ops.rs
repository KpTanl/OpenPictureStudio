use super::*;

impl State {
    pub(super) fn image_check(&self, image: &GpuImage) -> PyResult<()> {
        self.check()?;
        if image.generation != self.generation || !std::ptr::eq(Arc::as_ptr(&image.storage.state), self) {
            return Err(error("device_changed", "GPU image belongs to another device"));
        }
        Ok(())
    }

    pub(super) fn image_op(self: &Arc<Self>, io: &mut Io, image: &GpuImage, other: &GpuImage, mode: u32, args: &[f32],
                width: usize, height: usize, cancelled: Option<Py<PyAny>>) -> PyResult<GpuImage> {
        let output = self.encode_image_op(io, image, other, mode, args, width, height)?;
        self.recorded(io, 1, cancelled)?;
        Ok(output)
    }

    pub(super) fn encode_image_op(self: &Arc<Self>, io: &mut Io,
                image: &GpuImage, other: &GpuImage, mode: u32, args: &[f32],
                width: usize, height: usize) -> PyResult<GpuImage> {
        self.image_check(image)?;
        self.image_check(other)?;
        if width == 0 || height == 0 || width.div_ceil(8) > self.limits.max_compute_workgroups_per_dimension as usize
            || height.div_ceil(8) > self.limits.max_compute_workgroups_per_dimension as usize || args.len() > 20 || mode > 15 {
            return Err(error("target_limit", "Invalid GPU operation target"));
        }
        if matches!(mode,4|6|14|15) && (other.width != width || other.height != height) {
            return Err(error("target_limit", "GPU auxiliary image size mismatch"));
        }
        if mode == 14 && (args.len() != 2 || args.iter().any(|v| !v.is_finite() || *v < 0.0 || v.fract() != 0.0)
            || args[0] as usize + width > image.width || args[1] as usize + height > image.height) {
            return Err(error("target_limit", "Invalid regional remap bounds"));
        }
        if mode == 15 && (args.len() != 6 || args.iter().any(|v| !v.is_finite() || *v < 0.0 || v.fract() != 0.0)
            || args[0] as usize + width > args[4] as usize || args[1] as usize + height > args[5] as usize
            || args[2] as usize + image.width > args[4] as usize || args[3] as usize + image.height > args[5] as usize) {
            return Err(error("target_limit", "Invalid cropped remap bounds"));
        }
        if mode==1 && (width.max(height)>32768 || args.len()!=16) {
            return Err(error("target_limit","Unsupported GPU affine specification"));
        }
        if matches!(mode,8|9) {
            let extent=if mode==8 {image.width} else {image.height};
            let count=if mode==8 {width} else {height};
            if args.len()!=2 || args.iter().any(|v|!v.is_finite()) || args[0]<0.0 || args[1]<=args[0]
                || args[1]>extent as f32 || 6.0*((args[1]-args[0])/count as f32).max(1.0)>4096.0 {
                return Err(error("target_limit","GPU resize filter exceeds submission limit"));
            }
        }
        let target = self.allocate((width * height * 16) as u64)?;
        let mut params = [0u32; 32];
        params[..12].copy_from_slice(&[image.width as u32, image.height as u32, image.stride as u32, image.offset as u32,
            other.width as u32, other.height as u32, other.stride as u32, other.offset as u32,
            width as u32, height as u32, mode, 0]);
        for (index, value) in args.iter().enumerate() { params[index + 12] = value.to_bits(); }
        let uniform = self.uniform("ops-image-params", bytemuck::cast_slice(&params))?;
        self.dispatch(self.encoder(io)?, "image_ops", &[image.storage.buffer.as_ref().unwrap(), other.storage.buffer.as_ref().unwrap(),
            target.buffer.as_ref().unwrap(), &uniform], ((width as u32).div_ceil(8), (height as u32).div_ceil(8)));
        Ok(GpuImage { storage: target, width, height, generation: self.generation, ticket: self.ticket(io), stride: width, offset: 0 })
    }
}

impl GpuDevice {
    pub(super) fn view_image(&self, image: &GpuImage, left: usize, top: usize, width: usize, height: usize) -> PyResult<GpuImage> {
        self.state.image_check(image)?;
        if width == 0 || height == 0 || left >= image.width || top >= image.height
            || width > image.width - left || height > image.height - top {
            return Err(error("target_limit", "Invalid GPU view"));
        }
        Ok(GpuImage { storage: image.storage.clone(), width, height, generation: image.generation,
            ticket: image.ticket.clone(), stride: image.stride, offset: image.offset + top * image.stride + left })
    }

    pub(super) fn run_image(&self, py: Python<'_>, image: &GpuImage, other: &GpuImage, mode: u32, args: Vec<f32>,
                width: usize, height: usize, cancelled: Option<Py<PyAny>>) -> PyResult<GpuImage> {
        py.allow_threads(|| {
            let mut io = self.state.io.lock().unwrap();
            self.state.image_op(&mut io, image, other, mode, &args, width, height, cancelled)
        })
    }

    pub(super) fn read_image<'py>(&self, py: Python<'py>, image: &GpuImage, left: usize, top: usize,
                        width: usize, height: usize, cancelled: Option<Py<PyAny>>) -> PyResult<Bound<'py, PyArray3<f32>>> {
        let view = self.view_image(image, left, top, width, height)?;
        let output = py.allow_threads(|| {
            let mut io = self.state.io.lock().unwrap();
            let packed = self.state.encode_image_op(&mut io, &view, &view, 0, &[], width, height)?;
            let size = (width * height * 16) as u64;
            let staging = self.state.read(&mut io, packed.storage.buffer.as_ref().unwrap(), size)?;
            let bytes = self.state.mapped(&mut io, &staging, size, cancelled)?;
            Ok::<_, PyErr>(bytes.chunks_exact(4).map(|value| f32::from_le_bytes(value.try_into().unwrap())).collect::<Vec<_>>())
        })?;
        PyArray1::from_vec_bound(py, output).reshape([height, width, 4])
    }
}
