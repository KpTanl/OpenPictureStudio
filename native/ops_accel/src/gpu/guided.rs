use super::*;

struct GuidedBatch<'a> {
    state: &'a Arc<State>,
    io: &'a mut Io,
    passes: u32,
    cancelled: Option<Py<PyAny>>,
    // 这次滤波里已经用完的中间图：后面同尺寸的输出先从这里取，不再每张都向驱动申请
    spare: Vec<Arc<Allocation>>,
}

/// 连续存放、从缓冲开头起的图，可以直接当滤波输入，不必先复制一份
fn compact(image: &GpuImage) -> bool { image.stride == image.width && image.offset == 0 }

impl GuidedBatch<'_> {
    fn target(&mut self, size: u64) -> PyResult<Arc<Allocation>> {
        match self.spare.iter().position(|item| item.size == size) {
            Some(index) => Ok(self.spare.swap_remove(index)),
            None => self.state.allocate(size),
        }
    }

    fn release(&mut self, image: GpuImage) {
        if Arc::strong_count(&image.storage) == 1 { self.spare.push(image.storage); }
    }

    fn image(&mut self, source: &GpuImage, mode: u32, width: usize, height: usize) -> PyResult<GpuImage> {
        self.passes += 1;
        self.state.encode_image_op(self.io, source, source, mode, &[], width, height)
    }

    fn resize(&mut self, source: &GpuImage, width: usize, height: usize, args: &[f32]) -> PyResult<GpuImage> {
        self.passes += 1;
        self.state.encode_image_op(self.io, source, source, 7, args, width, height)
    }

    fn guide(&mut self, a: &GpuImage, b: &GpuImage, c: &GpuImage, mode: u32,
             eps: f32, center_g: &GpuImage, center_p: &GpuImage) -> PyResult<GpuImage> {
        let target = self.target((a.width*a.height*16) as u64)?;
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
            let target = self.target((image.width*image.height*16) as u64)?;
            let params = [image.width as u32, image.height as u32, radius, axis];
            let uniform = self.state.uniform("ops-box-params", bytemuck::cast_slice(&params))?;
            let lines = if axis == 0 { image.height } else { image.width };
            self.passes += 1;
            self.state.dispatch(self.state.encoder(self.io)?, "box_vec", &[
                input.buffer.as_ref().unwrap(), target.buffer.as_ref().unwrap(), &uniform],
                ((lines as u32).div_ceil(16), 1));
            // 第一遍的结果读完就不用了；调用方传进来的图另有引用，不会放进备用列表
            let previous = std::mem::replace(&mut input, target);
            if Arc::strong_count(&previous) == 1 { self.spare.push(previous); }
        }
        Ok(GpuImage { storage: input, width: image.width, height: image.height, generation: self.state.generation,
            ticket: self.state.ticket(self.io), stride: image.width, offset: 0 })
    }

    fn finish(self, output: GpuImage) -> PyResult<GpuImage> {
        self.state.recorded(self.io, self.passes, self.cancelled)?;
        Ok(output)
    }
    /// 按整幅坐标双线性缩放：source 是整幅 source_full 里从 source_origin 起的一块，
    /// 输出是整幅 output_full 里从 output_origin 起的 width×height 一块。
    fn resample(&mut self, source: &GpuImage, width: usize, height: usize, source_origin: (usize, usize),
                source_full: (usize, usize), output_origin: (usize, usize), output_full: (usize, usize))
                -> PyResult<GpuImage> {
        self.resize(source, width, height, &[source_origin.0 as f32, source_origin.1 as f32,
            source_full.0 as f32, source_full.1 as f32, output_origin.0 as f32, output_origin.1 as f32,
            output_full.0 as f32, output_full.1 as f32])
    }

    /// 低分辨率引导图 low_g、输入 low_p 上的系数 a、b（已做方框滤波）。
    fn coefficients(&mut self, low_g: &GpuImage, low_p: &GpuImage, r: u32, eps: f32) -> PyResult<(GpuImage, GpuImage)> {
        let stats = self.guide(low_g, low_p, low_g, 0, eps, low_g, low_p)?;
        let mean_g = self.box_filter(&stats, r)?;
        self.release(stats);
        let centered_p = self.guide(low_g, low_p, low_g, 5, eps, low_g, low_p)?;
        let mean_p = self.box_filter(&centered_p, r)?;
        self.release(centered_p);
        let product = self.guide(low_g, low_p, low_g, 1, eps, low_g, low_p)?;
        let mean_ip = self.box_filter(&product, r)?;
        self.release(product);
        let a = self.guide(&mean_g, &mean_p, &mean_ip, 2, eps, low_g, low_p)?;
        self.release(mean_ip);
        let b = self.guide(&mean_g, &mean_p, &a, 3, eps, low_g, low_p)?;
        self.release(mean_g);
        self.release(mean_p);
        let mean_a = self.box_filter(&a, r)?;
        self.release(a);
        let mean_b = self.box_filter(&b, r)?;
        self.release(b);
        Ok((mean_a, mean_b))
    }

    /// 把整幅低分辨率系数放大到引导图这一块（整幅 full 里从 origin 起）上，输出 q = a·I + b。
    /// center 的第一个像素是整幅低分辨率引导图的第一个像素，各块用同一个值居中。
    fn apply(&mut self, guide: &GpuImage, a: &GpuImage, b: &GpuImage, center: &GpuImage, eps: f32,
             origin: (usize, usize), full: (usize, usize)) -> PyResult<GpuImage> {
        let a = self.resample(a, guide.width, guide.height, (0, 0), (a.width, a.height), origin, full)?;
        let b = self.resample(b, guide.width, guide.height, (0, 0), (b.width, b.height), origin, full)?;
        if compact(guide) {
            return self.guide(guide, &a, &b, 4, eps, center, center);
        }
        let full_g = self.image(guide, 0, guide.width, guide.height)?;
        self.guide(&full_g, &a, &b, 4, eps, center, center)
    }

    fn run(mut self, image: &GpuImage, guide: &GpuImage, radius: u32, eps: f32, subsample: usize) -> PyResult<GpuImage> {
        if radius == 0 {
            let output = self.image(image, 0, image.width, image.height)?;
            return self.finish(output);
        }
        let full = (image.width, image.height);
        let width = (image.width/subsample).max(1);
        let height = (image.height/subsample).max(1);
        // 不降采样且输入连续存放时直接拿原图算，不复制
        let direct = subsample == 1 && compact(guide) && compact(image);
        let copies;
        let (low_g, low_p) = if direct { (guide, image) } else {
            copies = if subsample > 1 {
                (self.resample(guide, width, height, (0, 0), full, (0, 0), (width, height))?,
                 self.resample(image, width, height, (0, 0), full, (0, 0), (width, height))?)
            } else {
                (self.image(guide, 0, width, height)?, self.image(image, 0, width, height)?)
            };
            (&copies.0, &copies.1)
        };
        let r = if subsample > 1 { (radius/subsample as u32).max(1) } else { radius };
        let (a, b) = self.coefficients(low_g, low_p, r, eps)?;
        let copied_g;
        let full_g = if compact(guide) { guide } else {
            copied_g = self.image(guide, 0, guide.width, guide.height)?;
            &copied_g
        };
        let (a, b) = if subsample > 1 {
            (self.resample(&a, image.width, image.height, (0, 0), (width, height), (0, 0), full)?,
             self.resample(&b, image.width, image.height, (0, 0), (width, height), (0, 0), full)?)
        } else { (a, b) };
        let output = self.guide(full_g, &a, &b, 4, eps, low_g, low_p)?;
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
            GuidedBatch { state: &self.state, io: &mut io, passes: 0, cancelled, spare: Vec::new() }.run(image, guide, radius, eps, subsample)
        })
    }

    /// 分条时第一步：整幅低分辨率引导图 low 上的系数 a、b，和 low 第一个像素拷出的 1×1 居中图。
    /// 与整幅 guided 里同一段计算逐位相同。
    pub(super) fn guided_coefficients_image(&self, py: Python<'_>, low: &GpuImage, radius: u32, eps: f32,
                                            subsample: usize, cancelled: Option<Py<PyAny>>)
                                            -> PyResult<(GpuImage, GpuImage, GpuImage)> {
        self.state.image_check(low)?;
        if !eps.is_finite() || eps <= 0.0 || !(2..=16).contains(&subsample) || radius == 0
            || radius / subsample as u32 > 512 || low.width.max(low.height) > 32768 {
            return Err(error("target_limit", "Unsupported guided coefficient specification"));
        }
        py.allow_threads(|| {
            let mut io = self.state.io.lock().unwrap();
            let mut batch = GuidedBatch { state: &self.state, io: &mut io, passes: 0, cancelled, spare: Vec::new() };
            let low = batch.image(low, 0, low.width, low.height)?;
            let (a, b) = batch.coefficients(&low, &low, (radius/subsample as u32).max(1), eps)?;
            let center = batch.image(&low, 0, 1, 1)?;
            batch.state.recorded(batch.io, batch.passes, batch.cancelled)?;
            Ok((a, b, center))
        })
    }

    /// 分条时第二步：引导图 guide 是整幅 full 里从 origin 起的一条，用整幅系数算出这一条的结果。
    pub(super) fn guided_apply_image(&self, py: Python<'_>, guide: &GpuImage, a: &GpuImage, b: &GpuImage,
                                     center: &GpuImage, eps: f32, frame: (usize, usize, usize, usize),
                                     cancelled: Option<Py<PyAny>>) -> PyResult<GpuImage> {
        for image in [guide, a, b, center] {
            self.state.image_check(image)?;
        }
        let (full_width, full_height, left, top) = frame;
        if !eps.is_finite() || eps <= 0.0 || a.width != b.width || a.height != b.height
            || a.width > full_width || a.height > full_height || full_width.max(full_height) > 32768
            || left + guide.width > full_width || top + guide.height > full_height {
            return Err(error("target_limit", "Unsupported guided apply specification"));
        }
        py.allow_threads(|| {
            let mut io = self.state.io.lock().unwrap();
            let mut batch = GuidedBatch { state: &self.state, io: &mut io, passes: 0, cancelled, spare: Vec::new() };
            let output = batch.apply(guide, a, b, center, eps, (left, top), (full_width, full_height))?;
            batch.finish(output)
        })
    }
}
