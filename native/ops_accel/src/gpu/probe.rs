use super::*;
use rayon::prelude::*;

struct Case {
    width: usize,
    height: usize,
    rgb: Vec<f32>,
    field: Vec<f32>,
    guide: Vec<f32>,
    buffers: Vec<Arc<Allocation>>,
    groups: Vec<(&'static str, wgpu::BindGroup, (u32, u32))>,
}

impl Case {
    fn new(state: &Arc<State>, width: usize, height: usize) -> PyResult<Self> {
        let count = width * height;
        let mut rgb = Vec::with_capacity(count * 3);
        let mut field = Vec::with_capacity(count * 2);
        for y in 0..height {
            for x in 0..width {
                rgb.extend_from_slice(&[(x % 251) as f32 / 250.0, (y % 239) as f32 / 238.0,
                    ((x * 17 + y * 29) % 257) as f32 / 256.0]);
                field.extend_from_slice(&[((y % 9) as f32 - 4.0) * 0.375, ((x % 7) as f32 - 3.0) * 0.625]);
            }
        }
        let guide = rgb.chunks_exact(3).map(|p| (p[0] + p[1] + p[2]) / 3.0).collect();
        let sizes = [12, 8, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 12, 4];
        let buffers: Vec<_> = sizes.iter().map(|size| state.allocate((count * size) as u64)).collect::<PyResult<_>>()?;
        let mut params = [0i32; 12];
        params[0] = width as i32; params[1] = height as i32;
        params[2] = width as i32; params[3] = height as i32;
        params[4] = 10; params[5] = 0.0016f32.to_bits() as i32;
        params[8] = width as i32; params[9] = height as i32;
        let uniform = state.uniform("ops-probe-params", bytemuck::cast_slice(&params))?;
        let cells = ((width as u32).div_ceil(64), height as u32);
        let mut groups = Vec::new();
        let mut add = |name: &'static str, indices: &[usize], counts| {
            let mut inputs: Vec<_> = indices.iter().map(|index| buffers[*index].buffer.as_ref().unwrap()).collect();
            inputs.push(&uniform);
            groups.push((name, state.group(name, &inputs), counts));
        };
        for (source, target) in [(2, 4), (5, 6)] {
            add("box_h", &[source, 3], ((height as u32).div_ceil(16), 1));
            add("box_v", &[3, target], ((width as u32).div_ceil(16), 1));
            if source == 2 { add("square", &[2, 5], cells); }
        }
        add("coefficients", &[4, 6, 7, 8], cells);
        for (source, target) in [(7, 9), (8, 10)] {
            add("box_h", &[source, 3], ((height as u32).div_ceil(16), 1));
            add("box_v", &[3, target], ((width as u32).div_ceil(16), 1));
        }
        add("combine", &[2, 9, 10, 11], cells);
        add("remap_cubic", &[0, 1, 12], cells);
        add("display_full", &[12, 13], cells);
        Ok(Self { width, height, rgb, field, guide, buffers, groups })
    }

    fn run(&self, state: &State, io: &mut Io, floats: bool) -> PyResult<Vec<u8>> {
        let buffer = |index: usize| self.buffers[index].buffer.as_ref().unwrap();
        state.queue.write_buffer(buffer(0), 0, bytemuck::cast_slice(&self.rgb));
        state.queue.write_buffer(buffer(1), 0, bytemuck::cast_slice(&self.field));
        state.queue.write_buffer(buffer(2), 0, bytemuck::cast_slice(&self.guide));
        let mut encoder = state.device.create_command_encoder(&Default::default());
        for (name, group, counts) in &self.groups {
            let mut pass = encoder.begin_compute_pass(&wgpu::ComputePassDescriptor { label: Some(name), timestamp_writes: None });
            pass.set_pipeline(&state.pipelines[name]);
            pass.set_bind_group(0, group, &[]);
            pass.dispatch_workgroups(counts.0, counts.1, 1);
        }
        let size = (self.width * self.height * 4) as u64;
        let source = if floats { 11 } else { 13 };
        let staging = state.read(io, &mut encoder, buffer(source), size)?;
        state.mapped(encoder, &staging, size, None)
    }

    fn cpu(&self, threads: usize) -> PyResult<(Vec<u8>, Vec<f32>)> {
        crate::thread_pools::install(Some(threads), || {
            let filtered = crate::guided_filter::fast_guided_filter(&self.guide, &self.guide, self.width, self.height, 10, 0.0016, 1);
            let image = crate::image_view::ImageView { width: self.width, height: self.height, rgb: &self.rgb, alpha: None, patches: Vec::new() };
            let mut rgb = vec![0.0; self.rgb.len()];
            rgb.par_chunks_exact_mut(3).enumerate().for_each(|(index, pixel)| {
                let (color, _) = crate::remap::sample(&image, index % self.width, index / self.width, &self.field[index * 2..index * 2 + 2]);
                pixel.copy_from_slice(&color);
            });
            let mut output = vec![0; self.width * self.height * 4];
            crate::display::linear_to_display_rgba8(&rgb, None, self.width, &mut output);
            (output, filtered)
        })
    }
}

impl State {
    pub(super) fn qualify(self: &Arc<Self>, threads: usize, admission: Option<&Py<PyAny>>) -> PyResult<(u8, f64, f64, f64)> {
        let mut io = self.io.lock().unwrap();
        let run_cpu = |case: &Case| -> PyResult<((Vec<u8>, Vec<f32>), f64)> {
            let context = admission.map(|factory| Python::with_gil(|py| {
                let context = factory.call0(py)?;
                let allocation = context.call_method0(py, "__enter__")?;
                let granted = allocation.getattr(py, "threads")?.extract::<usize>(py)?;
                Ok::<_, PyErr>((context, granted))
            })).transpose()?;
            let started = Instant::now();
            let result = case.cpu(context.as_ref().map_or(threads, |entry| entry.1));
            let elapsed = started.elapsed().as_secs_f64() * 1000.0;
            if let Some((context, _)) = context {
                Python::with_gil(|py| context.call_method1(py, "__exit__", (py.None(), py.None(), py.None())))?;
            }
            result.map(|value| (value, elapsed))
        };
        let small = Case::new(self, 256, 256)?;
        let ((reference, filtered), _) = run_cpu(&small)?;
        let output = small.run(self, &mut io, false)?;
        let mut difference = output.iter().zip(&reference).map(|(a, b)| a.abs_diff(*b)).max().unwrap_or(255);
        let output = small.run(self, &mut io, true)?;
        for (bytes, expected) in output.chunks_exact(4).zip(filtered) {
            let value = f32::from_le_bytes(bytes.try_into().unwrap());
            if !value.is_finite() { return Err(error("self_check", "Non-finite GPU filter output")); }
            difference = difference.max(crate::display::pixel([value; 3], 1.0)[0].abs_diff(crate::display::pixel([expected; 3], 1.0)[0]));
        }
        difference = difference.max(self.check_alpha_display(&mut io, &small)?);
        difference = difference.max(self.check_area_display(&mut io, &small)?);
        drop(small);
        if difference > 1 { return Ok((difference, 0.0, f64::INFINITY, 0.0)); }
        let large = Case::new(self, 1600, 1000)?;
        run_cpu(&large)?;
        let mut cpu = Vec::new();
        for _ in 0..3 {
            cpu.push(run_cpu(&large)?.1);
        }
        cpu.sort_by(f64::total_cmp);
        let start = Instant::now();
        large.run(self, &mut io, false)?;
        let warmup = start.elapsed().as_secs_f64() * 1000.0;
        if warmup > cpu[1] * 10.0 { return Ok((difference, cpu[1], warmup, warmup)); }
        let mut gpu = Vec::new();
        for _ in 0..3 {
            let start = Instant::now();
            large.run(self, &mut io, false)?;
            gpu.push(start.elapsed().as_secs_f64() * 1000.0);
        }
        gpu.sort_by(f64::total_cmp);
        Ok((difference, cpu[1], gpu[1], warmup))
    }

    fn check_alpha_display(self: &Arc<Self>, io: &mut Io, case: &Case) -> PyResult<u8> {
        let alpha: Vec<_> = (0..case.width * case.height).map(|index| (index % 17) as f32 / 16.0).collect();
        let packed: Vec<_> = case.rgb.chunks_exact(3).zip(&alpha).flat_map(|(rgb, a)| [rgb[0], rgb[1], rgb[2], *a]).collect();
        let source = self.allocate((packed.len() * 4) as u64)?;
        let target = self.allocate(127 * 93 * 4)?;
        self.queue.write_buffer(source.buffer.as_ref().unwrap(), 0, bytemuck::cast_slice(&packed));
        let params = [case.width as u32, case.height as u32, 127, 93, case.width as u32, 0, 0, 0];
        let uniform = self.uniform("ops-alpha-check", bytemuck::cast_slice(&params))?;
        let mut encoder = self.device.create_command_encoder(&Default::default());
        self.dispatch(&mut encoder, "display_rgba", &[source.buffer.as_ref().unwrap(), target.buffer.as_ref().unwrap(), &uniform], (16, 12));
        let staging = self.read(io, &mut encoder, target.buffer.as_ref().unwrap(), 127 * 93 * 4)?;
        let output = self.mapped(encoder, &staging, 127 * 93 * 4, None)?;
        let mut full = vec![0; alpha.len() * 4];
        crate::display::linear_to_display_rgba8(&case.rgb, Some(&alpha), case.width, &mut full);
        let reference = crate::display_output::area(&full, case.width, case.height, 127, 93, &mut Cancellation::new(None))?;
        Ok(output.iter().zip(reference).map(|(a, b)| a.abs_diff(b)).max().unwrap_or(255))
    }

    fn check_area_display(self: &Arc<Self>, io: &mut Io, case: &Case) -> PyResult<u8> {
        let target = self.allocate(127 * 93 * 4)?;
        let mut params = [0i32; 12];
        params[0] = case.width as i32; params[1] = case.height as i32;
        params[2] = 127; params[3] = 93;
        let uniform = self.uniform("ops-area-check", bytemuck::cast_slice(&params))?;
        let mut encoder = self.device.create_command_encoder(&Default::default());
        self.dispatch(&mut encoder, "display_area", &[case.buffers[0].buffer.as_ref().unwrap(), target.buffer.as_ref().unwrap(), &uniform], (2, 93));
        let staging = self.read(io, &mut encoder, target.buffer.as_ref().unwrap(), 127 * 93 * 4)?;
        let output = self.mapped(encoder, &staging, 127 * 93 * 4, None)?;
        let mut full = vec![0; case.width * case.height * 4];
        crate::display::linear_to_display_rgba8(&case.rgb, None, case.width, &mut full);
        let reference = crate::display_output::area(&full, case.width, case.height, 127, 93, &mut Cancellation::new(None))?;
        Ok(output.iter().zip(reference).map(|(a, b)| a.abs_diff(b)).max().unwrap_or(255))
    }
}
