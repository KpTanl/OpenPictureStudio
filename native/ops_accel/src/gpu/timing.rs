use super::*;

pub(super) struct Timing {
    queries: Vec<wgpu::QuerySet>,
    labels: Vec<String>,
    pub thread: std::thread::ThreadId,
    pub waits: usize,
    skipped: usize,
}

impl Timing {
    pub fn reserve(&mut self, label: &str) -> Option<wgpu::ComputePassTimestampWrites<'_>> {
        if self.thread != std::thread::current().id() { return None; }
        let index = self.labels.len() as u32 * 2;
        let page = index / wgpu::QUERY_SET_MAX_QUERIES;
        if page as usize >= self.queries.len() {
            self.skipped += 1;
            return None;
        }
        self.labels.push(label.to_owned());
        let index = index % wgpu::QUERY_SET_MAX_QUERIES;
        Some(wgpu::ComputePassTimestampWrites { query_set: &self.queries[page as usize],
            beginning_of_pass_write_index: Some(index), end_of_pass_write_index: Some(index + 1) })
    }
}

impl GpuDevice {
    pub(super) fn begin_timing(&self) -> PyResult<()> {
        let _io = self.state.io.lock().unwrap();
        self.state.check()?;
        if !self.state.device.features().contains(wgpu::Features::TIMESTAMP_QUERY) {
            return Err(error("profiling_unsupported", "GPU timestamp queries are unavailable"));
        }
        let mut timing = self.state.timing.lock().unwrap();
        if timing.is_some() { return Err(error("profiling_active", "GPU timing is already active")); }
        *timing = Some(Timing {
            queries: (0..4).map(|_| self.state.device.create_query_set(&wgpu::QuerySetDescriptor {
                label: Some("ops-timestamps"), ty: wgpu::QueryType::Timestamp, count: wgpu::QUERY_SET_MAX_QUERIES })).collect(),
            labels: Vec::new(), thread: std::thread::current().id(), waits: 0, skipped: 0,
        });
        self.state.check()
    }

    pub(super) fn end_timing<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let (timing, bytes) = py.allow_threads(|| {
            let mut io = self.state.io.lock().unwrap();
            let timing = self.state.timing.lock().unwrap().take()
                .ok_or_else(|| error("profiling_inactive", "GPU timing is not active"))?;
            let count = timing.labels.len() as u32 * 2;
            if count == 0 { return Ok((timing, Vec::new())); }
            let size = count as u64 * 8;
            let resolved = self.state.device.create_buffer(&wgpu::BufferDescriptor {
                label: Some("ops-resolved-timestamps"), size,
                usage: wgpu::BufferUsages::QUERY_RESOLVE | wgpu::BufferUsages::COPY_SRC, mapped_at_creation: false });
            self.state.check()?;
            let mut encoder = self.state.device.create_command_encoder(&Default::default());
            for (page, queries) in timing.queries.iter().enumerate() {
                let first = page as u32 * wgpu::QUERY_SET_MAX_QUERIES;
                if first >= count { break; }
                encoder.resolve_query_set(queries, 0..(count-first).min(wgpu::QUERY_SET_MAX_QUERIES), &resolved, first as u64 * 8);
            }
            let staging = self.state.read(&mut io, &mut encoder, &resolved, size)?;
            let bytes = self.state.mapped(encoder, &staging, size, None)?;
            Ok::<_, PyErr>((timing, bytes))
        })?;
        let period = self.state.queue.get_timestamp_period() as f64 / 1_000_000.0;
        let passes: Vec<_> = timing.labels.into_iter().zip(bytes.chunks_exact(16)).map(|(label, values)| {
            let first = u64::from_le_bytes(values[..8].try_into().unwrap());
            let last = u64::from_le_bytes(values[8..].try_into().unwrap());
            (label, last.saturating_sub(first) as f64 * period)
        }).collect();
        let result = PyDict::new_bound(py);
        result.set_item("passes", passes)?;
        result.set_item("waits", timing.waits)?;
        result.set_item("skipped", timing.skipped)?;
        Ok(result)
    }
}
