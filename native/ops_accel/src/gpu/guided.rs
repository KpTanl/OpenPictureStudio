use super::*;

struct GuidedBatch<'a> {
    state: &'a Arc<State>,
    io: &'a mut Io,
    passes: u32,
    cancelled: Option<Py<PyAny>>,
}

impl GuidedBatch<'_> {
    fn image(&mut self, source: &GpuImage, mode: u32, width: usize, height: usize) -> PyResult<GpuImage> {
        self.passes += 1;
        self.state.encode_image_op(self.io, source, source, mode, &[], width, height)
    }

    fn guide(&mut self, a: &GpuImage, b: &GpuImage, c: &GpuImage, mode: u32,
             eps: f32, center_g: &GpuImage, center_p: &GpuImage) -> PyResult<GpuImage> {
        let target = self.state.allocate((a.width*a.height*16) as u64)?;
        let params = [a.width as u32, a.height as u32, mode, eps.to_bits()];
        let uniform = self.state.uniform("ops-guided-params", bytemuck::cast_slice(&params))?;
        self.passes += 1;
        self.state.dispatch_named(self.state.encoder(self.io)?, "guided", &format!("guided.{mode}"), &[
            a.storage.buffer.as_ref().unwrap(), b.storage.buffer.as_ref().unwrap(), c.storage.buffer.as_ref().unwrap(),
            center_g.storage.buffer.as_ref().unwrap(), center_p.storage.buffer.as_ref().unwrap(),
            target.buffer.as_ref().unwrap(), &uniform], ((a.width as u32).div_ceil(8), (a.height as u32).div_ceil(8)));
        Ok(GpuImage { storage: target, width: a.width, height: a.height, generation: self.state.generation,
            ticket: self.state.ticket(self.io), stride: a.width, offset: 0 })
    }

    fn box_filter(&mut self, image: &GpuImage, radius: u32) -> PyResult<GpuImage> {
        let mut input = image.storage.clone();
        for axis in 0..2 {
            let target = self.state.allocate((image.width*image.height*16) as u64)?;
            let params = [image.width as u32, image.height as u32, radius, axis];
            let uniform = self.state.uniform("ops-box-params", bytemuck::cast_slice(&params))?;
            let lines = if axis == 0 { image.height } else { image.width };
            self.passes += 1;
            self.state.dispatch(self.state.encoder(self.io)?, "box_vec", &[
                input.buffer.as_ref().unwrap(), target.buffer.as_ref().unwrap(), &uniform],
                ((lines as u32).div_ceil(16), 1));
            input = target;
        }
        Ok(GpuImage { storage: input, width: image.width, height: image.height, generation: self.state.generation,
            ticket: self.state.ticket(self.io), stride: image.width, offset: 0 })
    }

    fn finish(self, output: GpuImage) -> PyResult<GpuImage> {
        self.state.recorded(self.io, self.passes, self.cancelled)?;
        Ok(output)
    }
    fn run(mut self, image: &GpuImage, guide: &GpuImage, radius: u32, eps: f32, subsample: usize) -> PyResult<GpuImage> {
        if radius == 0 {
            let output = self.image(image, 0, image.width, image.height)?;
            return self.finish(output);
        }
        let width = (image.width/subsample).max(1);
        let height = (image.height/subsample).max(1);
        let low_g = self.image(guide, if subsample > 1 { 7 } else { 0 }, width, height)?;
        let low_p = self.image(image, if subsample > 1 { 7 } else { 0 }, width, height)?;
        let r = if subsample > 1 { (radius/subsample as u32).max(1) } else { radius };
        let stats = self.guide(&low_g, &low_p, &low_g, 0, eps, &low_g, &low_p)?;
        let mean_g = self.box_filter(&stats, r)?;
        let centered_p = self.guide(&low_g, &low_p, &low_g, 5, eps, &low_g, &low_p)?;
        let mean_p = self.box_filter(&centered_p, r)?;
        let product = self.guide(&low_g, &low_p, &low_g, 1, eps, &low_g, &low_p)?;
        let mean_ip = self.box_filter(&product, r)?;
        let a = self.guide(&mean_g, &mean_p, &mean_ip, 2, eps, &low_g, &low_p)?;
        let b = self.guide(&mean_g, &mean_p, &a, 3, eps, &low_g, &low_p)?;
        let a = self.box_filter(&a, r)?;
        let b = self.box_filter(&b, r)?;
        let a = if subsample > 1 { self.image(&a, 7, image.width, image.height)? } else { a };
        let b = if subsample > 1 { self.image(&b, 7, image.width, image.height)? } else { b };
        let full_g = self.image(guide, 0, guide.width, guide.height)?;
        let output = self.guide(&full_g, &a, &b, 4, eps, &low_g, &low_p)?;
        self.finish(output)
    }

}

impl GpuDevice {
    pub(super) fn guided_image(&self, py: Python<'_>, image: &GpuImage, guide: &GpuImage, radius: u32,
                               eps: f32, subsample: usize, cancelled: Option<Py<PyAny>>) -> PyResult<GpuImage> {
        self.state.image_check(image)?;
        self.state.image_check(guide)?;
        if image.width != guide.width || image.height != guide.height || !eps.is_finite() || eps <= 0.0
            || !(1..=16).contains(&subsample) || radius / subsample.max(1) as u32 > 512
            || image.width.max(image.height) > 32768 {
            return Err(error("target_limit", "Unsupported guided filter specification"));
        }
        py.allow_threads(|| {
            let mut io = self.state.io.lock().unwrap();
            GuidedBatch { state: &self.state, io: &mut io, passes: 0, cancelled }.run(image, guide, radius, eps, subsample)
        })
    }
}
