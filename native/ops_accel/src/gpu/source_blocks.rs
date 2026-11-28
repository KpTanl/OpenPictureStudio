use super::*;

impl State {
    pub(super) fn source_blocks(self: &Arc<Self>, io: &mut Io, data: &[u8], blocks: &[u32], expected: usize,
        cancelled: Option<Py<PyAny>>) -> PyResult<Arc<Allocation>> {
        crate::source_cache::validate_blocks(data, blocks, expected)
            .map_err(|reason| error("source_blocks", reason))?;
        if !self.pipelines.contains_key("source_blocks") {
            return Err(error("source_blocks_capability", "GPU source block kernel is unavailable"));
        }
        let clone_cancel = || cancelled.as_ref().map(|value| Python::with_gil(|py| value.clone_ref(py)));
        self.flush(io, clone_cancel())?;
        let packed = self.allocate(data.len() as u64)?;
        let index = self.allocate((blocks.len() * 4) as u64)?;
        let result = self.allocate(expected as u64)?;
        let status = self.allocate(blocks.len() as u64)?;
        {
            let mut pool = self.pool.lock().unwrap();
            let staging = (data.len() + blocks.len() * 4) as u64;
            if pool.total() + staging > pool.limit { pool.idle.clear(); }
            if pool.total() + staging > pool.limit { return Err(error("memory_pressure", "Source block staging exceeds budget")); }
            pool.transient = staging;
        }
        let upload = {
            self.queue.write_buffer(packed.buffer.as_ref().unwrap(), 0, data);
            self.queue.write_buffer(index.buffer.as_ref().unwrap(), 0, bytemuck::cast_slice(blocks));
            self.wait(self.queue.submit([]), clone_cancel())
        };
        self.pool.lock().unwrap().transient = 0;
        upload?;
        self.uploaded.fetch_add((data.len() + blocks.len() * 4) as u64, Ordering::Relaxed);
        let count = blocks.len() / 4;
        for start in (0..count).step_by(64) {
            Cancellation::new(clone_cancel()).check()?;
            let uniform = self.uniform("ops-source-block-batch", bytemuck::cast_slice(&[start as u32, 0, 0, 0]))?;
            self.dispatch(self.encoder(io)?, "source_blocks", &[packed.buffer.as_ref().unwrap(), index.buffer.as_ref().unwrap(),
                result.buffer.as_ref().unwrap(), status.buffer.as_ref().unwrap(), &uniform], ((count - start).min(64) as u32, 1));
            self.flush(io, clone_cancel())?;
        }
        let staging = self.read(io, status.buffer.as_ref().unwrap(), blocks.len() as u64)?;
        let values = self.mapped(io, &staging, blocks.len() as u64, clone_cancel())?;
        if values.iter().any(|value| *value != 0) { return Err(error("source_blocks", "Invalid compressed source block")); }
        Ok(result)
    }
}
