use std::{collections::HashMap, sync::{Arc, Mutex, atomic::{AtomicBool, AtomicU64, Ordering}}, time::{Duration, Instant}};
use numpy::{PyArray1, PyArray3, PyArrayMethods, PyReadonlyArray2, PyReadonlyArray3, PyUntypedArrayMethods};
use pyo3::{prelude::*, types::PyDict};

use crate::cancellation::Cancellation;

mod probe;
mod image_ops;
mod source_input;
mod source_blocks;
mod color;
mod beauty;
mod timing;
#[cfg(windows)]
mod face;
mod guided;
mod masks;
#[cfg(windows)]
mod interop;
#[cfg(windows)]
mod ort;
#[cfg(windows)]
pub use ort::{GpuModel, GpuTensor};

pyo3::create_exception!(ops_accel, GpuError, pyo3::exceptions::PyRuntimeError);
pub const REVISION: &str = "wgpu27-dx12-beauty-7";
const PENDING: u64 = u64::MAX;
const PASS_LIMIT: u32 = 512;
const TRANSIENT_LIMIT: u64 = 64 << 20;
const RETIRED_LIMIT: u64 = 512 << 20;
const STORAGE: wgpu::BufferUsages = wgpu::BufferUsages::from_bits_retain(
    wgpu::BufferUsages::STORAGE.bits() | wgpu::BufferUsages::COPY_SRC.bits() | wgpu::BufferUsages::COPY_DST.bits());

const SHADERS: &[(&str, &str)] = &[
    ("source_blocks", include_str!("shaders/source_blocks.wgsl")),
    ("png_restore", include_str!("shaders/png_restore.wgsl")),
    ("source_input", include_str!("shaders/source_input.wgsl")),
    ("color", include_str!("shaders/color.wgsl")),
    ("beauty", include_str!("shaders/beauty.wgsl")),
    ("beauty_stats", include_str!("shaders/beauty_stats.wgsl")),
    ("beauty_median", include_str!("shaders/beauty_median.wgsl")),
    ("beauty_partition", include_str!("shaders/beauty_partition.wgsl")),
    ("beauty_components", include_str!("shaders/beauty_components.wgsl")),
    ("beauty_spots", include_str!("shaders/beauty_spots.wgsl")),
    ("enhance", include_str!("shaders/enhance.wgsl")),
    ("legacy", include_str!("shaders/legacy.wgsl")),
    ("face", include_str!("shaders/face.wgsl")),
    ("face_regions", include_str!("shaders/face_regions.wgsl")),
    ("image_ops", include_str!("shaders/image_ops.wgsl")),
    ("guided", include_str!("shaders/guided.wgsl")),
    ("box_vec", include_str!("shaders/box_vec.wgsl")),
    ("ellipse_spans", include_str!("shaders/ellipse_spans.wgsl")),
    ("polygon_mask", include_str!("shaders/polygon_mask.wgsl")),
    ("feather", include_str!("shaders/feather.wgsl")),
    ("tensor_transfer", include_str!("shaders/tensor_transfer.wgsl")),
    ("tensor_clear", include_str!("shaders/tensor_clear.wgsl")),
    ("interop_prepare", include_str!("shaders/interop_prepare.wgsl")),
    ("display_rgba", include_str!("shaders/display_rgba.wgsl")),
    ("display_full", include_str!("shaders/display_full.wgsl")),
    ("display_area", include_str!("shaders/display_area.wgsl")),
    ("box_h", include_str!("shaders/box_h.wgsl")),
    ("box_v", include_str!("shaders/box_v.wgsl")),
    ("square", include_str!("shaders/square.wgsl")),
    ("coefficients", include_str!("shaders/coefficients.wgsl")),
    ("combine", include_str!("shaders/combine.wgsl")),
    ("remap_cubic", include_str!("shaders/remap_cubic.wgsl")),
];

fn done(event: u64) -> Arc<AtomicU64> { Arc::new(AtomicU64::new(event)) }

fn error(code: &str, message: impl ToString) -> PyErr {
    GpuError::new_err((code.to_owned(), message.to_string()))
}

fn instance(compiler: wgpu::Dx12Compiler) -> wgpu::Instance {
    let mut options = wgpu::InstanceDescriptor::default();
    options.backends = wgpu::Backends::DX12;
    options.backend_options.dx12.shader_compiler = compiler;
    options.memory_budget_thresholds = wgpu::MemoryBudgetThresholds {
        for_resource_creation: Some(90), for_device_loss: Some(98),
    };
    wgpu::Instance::new(&options)
}

fn hardware(adapter: &wgpu::Adapter) -> bool {
    matches!(adapter.get_info().device_type, wgpu::DeviceType::DiscreteGpu | wgpu::DeviceType::IntegratedGpu)
        && adapter.get_info().backend == wgpu::Backend::Dx12
}

#[cfg(windows)]
fn dxgi(adapter: &wgpu::Adapter) -> (String, u64, u64, u64) {
    unsafe {
        use windows::Win32::Graphics::Dxgi::DXGI_MEMORY_SEGMENT_GROUP_LOCAL;
        if let Some(raw) = adapter.as_hal::<wgpu::hal::api::Dx12>() {
            let native = raw.raw_adapter();
            if let Ok(desc) = native.GetDesc1() {
                let luid = format!("{:08x}{:08x}", desc.AdapterLuid.HighPart as u32, desc.AdapterLuid.LowPart);
                let memory = native.query_video_memory_info(DXGI_MEMORY_SEGMENT_GROUP_LOCAL).ok();
                return (luid, desc.DedicatedVideoMemory as u64,
                    memory.as_ref().map_or(0, |value| value.Budget), memory.as_ref().map_or(0, |value| value.CurrentUsage));
            }
        }
    }
    (String::new(), 0, 0, 0)
}

#[cfg(not(windows))]
fn dxgi(_: &wgpu::Adapter) -> (String, u64, u64, u64) { (String::new(), 0, 0, 0) }

fn adapter_record<'py>(py: Python<'py>, index: usize, adapter: &wgpu::Adapter) -> PyResult<Bound<'py, PyDict>> {
    let info = adapter.get_info();
    let (luid, dedicated, budget, usage) = dxgi(adapter);
    let record = PyDict::new_bound(py);
    record.set_item("index", index)?;
    record.set_item("name", info.name)?;
    record.set_item("vendor", info.vendor)?;
    record.set_item("device", info.device)?;
    record.set_item("kind", format!("{:?}", info.device_type))?;
    record.set_item("backend", format!("{:?}", info.backend))?;
    record.set_item("driver", info.driver)?;
    record.set_item("driver_info", info.driver_info)?;
    record.set_item("luid", luid)?;
    record.set_item("dedicated_bytes", dedicated)?;
    record.set_item("process_budget", budget)?;
    record.set_item("process_usage", usage)?;
    record.set_item("hardware", hardware(adapter))?;
    Ok(record)
}

#[pyfunction]
pub fn gpu_adapters(py: Python<'_>) -> PyResult<Vec<Py<PyDict>>> {
    let adapters = py.allow_threads(|| instance(wgpu::Dx12Compiler::Fxc).enumerate_adapters(wgpu::Backends::DX12));
    adapters.iter().enumerate().map(|(index, adapter)| adapter_record(py, index, adapter).map(|value| value.unbind())).collect()
}

#[derive(Default)]
struct Pool {
    active: u64,
    idle: Vec<(u64, wgpu::Buffer)>,
    staging: u64,
    transient: u64,
    retired: u64,
    recording: bool,
    limit: u64,
    idle_limit: u64,
}

impl Pool {
    fn idle_bytes(&self) -> u64 { self.idle.iter().map(|entry| entry.0).sum() }
    fn total(&self) -> u64 { self.active + self.idle_bytes() + self.staging + self.transient + self.retired }
}

#[derive(Default)]
struct Io {
    staging: Option<wgpu::Buffer>,
    size: u64,
    encoder: Option<wgpu::CommandEncoder>,
    passes: u32,
    owner: Option<std::thread::ThreadId>,
    ticket: Option<Arc<AtomicU64>>,
}

struct State {
    adapter: wgpu::Adapter,
    device: wgpu::Device,
    queue: wgpu::Queue,
    limits: wgpu::Limits,
    pipelines: HashMap<&'static str, wgpu::ComputePipeline>,
    compilation_ms: HashMap<&'static str, f64>,
    optional_failures: HashMap<&'static str, String>,
    shader_compiler: &'static str,
    first_submission_ms: Mutex<HashMap<String, f64>>,
    pending_kernels: Mutex<Vec<(String, Instant)>>,
    timing: Mutex<Option<timing::Timing>>,
    io: Mutex<Io>,
    pool: Mutex<Pool>,
    lost: Arc<AtomicBool>,
    failure: Arc<Mutex<Option<(String, String)>>>,
    completed: AtomicU64,
    submission_ms: AtomicU64,
    uploaded: AtomicU64,
    downloaded: AtomicU64,
    generation: u64,
}

struct Allocation { buffer: Option<wgpu::Buffer>, size: u64, state: Arc<State> }

impl Drop for Allocation {
    fn drop(&mut self) {
        {
            let state = &self.state;
            let mut pool = state.pool.lock().unwrap();
            pool.active -= self.size;
            if !state.lost.load(Ordering::Acquire) && pool.idle_bytes() + self.size <= pool.idle_limit {
                if let Some(buffer) = self.buffer.take() { pool.idle.push((self.size, buffer)); }
            } else if pool.recording {
                pool.retired += self.size;
            }
        }
    }
}

#[pyclass(frozen)]
pub struct GpuImage {
    storage: Arc<Allocation>,
    width: usize,
    height: usize,
    generation: u64,
    ticket: Arc<AtomicU64>,
    stride: usize,
    offset: usize,
}

#[pymethods]
impl GpuImage {
    #[getter]
    fn size(&self) -> (usize, usize) { (self.width, self.height) }
    #[getter]
    fn nbytes(&self) -> u64 { self.storage.size }
    #[getter]
    fn generation(&self) -> u64 { self.generation }
    #[getter]
    fn completion(&self) -> u64 { self.ticket.load(Ordering::Acquire) }
    #[getter]
    fn allocation_id(&self) -> usize { Arc::as_ptr(&self.storage) as usize }
    #[getter]
    fn ready(&self) -> bool {
        let event = self.ticket.load(Ordering::Acquire);
        event != PENDING && !self.storage.state.lost.load(Ordering::Acquire) && self.storage.state.completed.load(Ordering::Acquire) >= event
    }
}

impl State {
    fn upload_data(self: &Arc<Self>, data: &[f32], width: usize, height: usize,
                   cancelled: Option<Py<PyAny>>) -> PyResult<GpuImage> {
        let mut io = self.io.lock().unwrap();
        self.check()?;
        self.flush(&mut io, cancelled.as_ref().map(|value| Python::with_gil(|py| value.clone_ref(py))))?;
        let storage = self.allocate((width * height * 16) as u64)?;
        {
            let mut pool = self.pool.lock().unwrap();
            if pool.total() + storage.size > pool.limit { pool.idle.clear(); }
            if pool.total() + storage.size > pool.limit { return Err(error("memory_pressure", "GPU upload staging exceeds budget")); }
            pool.transient = storage.size;
        }
        let result = (|| {
            self.queue.write_buffer(storage.buffer.as_ref().unwrap(), 0, bytemuck::cast_slice(data));
            let event = self.wait(self.queue.submit([]), cancelled)?;
            self.uploaded.fetch_add(storage.size, Ordering::Relaxed);
            Ok(GpuImage { storage: storage.clone(), width, height, generation: self.generation, ticket: done(event), stride: width, offset: 0 })
        })();
        self.pool.lock().unwrap().transient = 0;
        result
    }

    fn check(&self) -> PyResult<()> {
        if self.lost.load(Ordering::Acquire) { return Err(error("device_lost", "GPU device is unavailable")); }
        if let Some((code, message)) = self.failure.lock().unwrap().take() { return Err(error(&code, message)); }
        Ok(())
    }

    fn allocate(self: &Arc<Self>, size: u64) -> PyResult<Arc<Allocation>> {
        self.check()?;
        let size = size.max(4).next_multiple_of(4);
        if size > self.limits.max_buffer_size || size > self.limits.max_storage_buffer_binding_size as u64 {
            return Err(error("target_limit", "GPU buffer exceeds adapter limits"));
        }
        let mut pool = self.pool.lock().unwrap();
        let candidate = pool.idle.iter().enumerate().filter(|(_, entry)| entry.0 == size).map(|(index, _)| index).next();
        let buffer = if let Some(index) = candidate { pool.idle.swap_remove(index).1 } else {
            if pool.total() + size > pool.limit { pool.idle.clear(); }
            if pool.total() + size > pool.limit { return Err(error("memory_pressure", "GPU working set exceeds budget")); }
            self.device.create_buffer(&wgpu::BufferDescriptor { label: Some("ops-storage"), size, usage: STORAGE, mapped_at_creation: false })
        };
        self.check()?;
        pool.active += size;
        Ok(Arc::new(Allocation { buffer: Some(buffer), size, state: self.clone() }))
    }

    fn uniform(&self, label: &str, data: &[u8]) -> PyResult<wgpu::Buffer> {
        self.check()?;
        let buffer = self.device.create_buffer(&wgpu::BufferDescriptor {
            label: Some(label), size: data.len() as u64,
            usage: wgpu::BufferUsages::UNIFORM | wgpu::BufferUsages::COPY_DST, mapped_at_creation: false,
        });
        self.check()?;
        self.queue.write_buffer(&buffer, 0, data);
        self.check()?;
        Ok(buffer)
    }

    fn data(&self, label: &str, data: &[f32]) -> PyResult<wgpu::Buffer> {
        use wgpu::util::DeviceExt;
        self.check()?;
        let size = (data.len().max(1) * 4) as u64;
        {
            let mut pool = self.pool.lock().unwrap();
            if pool.total() + size > pool.limit { pool.idle.clear(); }
            if pool.total() + size > pool.limit { return Err(error("memory_pressure", "GPU metadata exceeds budget")); }
            pool.transient += size;
        }
        let buffer = self.device.create_buffer_init(&wgpu::util::BufferInitDescriptor { label: Some(label),
            contents: if data.is_empty() { &[0u8; 4] } else { bytemuck::cast_slice(data) }, usage: STORAGE });
        self.check()?;
        Ok(buffer)
    }

    fn encoder<'a>(&self, io: &'a mut Io) -> PyResult<&'a mut wgpu::CommandEncoder> {
        let thread = std::thread::current().id();
        if io.encoder.is_some() && (io.owner != Some(thread) || self.pool.lock().unwrap().retired >= RETIRED_LIMIT) {
            self.flush(io, None)?;
        }
        io.owner = Some(thread);
        if io.encoder.is_none() { self.pool.lock().unwrap().recording = true; }
        Ok(io.encoder.get_or_insert_with(|| self.device.create_command_encoder(&Default::default())))
    }

    fn ticket(&self, io: &mut Io) -> Arc<AtomicU64> {
        io.ticket.get_or_insert_with(|| Arc::new(AtomicU64::new(PENDING))).clone()
    }

    fn recorded(&self, io: &mut Io, passes: u32, cancelled: Option<Py<PyAny>>) -> PyResult<()> {
        io.passes += passes;
        let full = {
            let pool = self.pool.lock().unwrap();
            pool.transient >= TRANSIENT_LIMIT || pool.retired >= RETIRED_LIMIT
        };
        if io.passes >= PASS_LIMIT || full { self.flush(io, cancelled)?; }
        Ok(())
    }

    fn discard(&self, io: &mut Io) {
        io.encoder = None;
        io.passes = 0;
        io.owner = None;
        io.ticket = None;
        let mut pool = self.pool.lock().unwrap();
        pool.transient = 0;
        pool.retired = 0;
        pool.recording = false;
    }

    fn launch(&self, io: &mut Io, cancelled: Option<Py<PyAny>>) -> PyResult<(Option<wgpu::SubmissionIndex>, Option<Arc<AtomicU64>>)> {
        if io.encoder.is_none() || io.owner == Some(std::thread::current().id()) {
            if let Err(reason) = self.check().and_then(|_| Cancellation::new(cancelled).check()) {
                self.discard(io);
                return Err(reason);
            }
        }
        let Some(encoder) = io.encoder.take() else { return Ok((None, None)); };
        io.passes = 0;
        io.owner = None;
        self.pool.lock().unwrap().recording = false;
        Ok((Some(self.queue.submit([encoder.finish()])), io.ticket.take()))
    }

    fn settle(&self, index: wgpu::SubmissionIndex, ticket: Option<Arc<AtomicU64>>, started: Instant) -> PyResult<u64> {
        let waited = self.wait(index, None);
        {
            let mut pool = self.pool.lock().unwrap();
            pool.transient = 0;
            pool.retired = 0;
        }
        let event = waited?;
        if let Some(ticket) = ticket { ticket.store(event, Ordering::Release); }
        let limit = self.submission_ms.load(Ordering::Relaxed);
        if limit != 0 && started.elapsed() > Duration::from_millis(limit) {
            return Err(error("submission_limit", "GPU submission exceeds the integrated-device limit"));
        }
        Ok(event)
    }

    fn flush(&self, io: &mut Io, cancelled: Option<Py<PyAny>>) -> PyResult<u64> {
        let started = Instant::now();
        match self.launch(io, cancelled)? {
            (Some(index), ticket) => self.settle(index, ticket, started),
            _ => Ok(self.completed.load(Ordering::Acquire)),
        }
    }

    fn wait(&self, index: wgpu::SubmissionIndex, cancelled: Option<Py<PyAny>>) -> PyResult<u64> {
        if let Some(timing) = self.timing.lock().unwrap().as_mut() {
            if timing.thread == std::thread::current().id() { timing.waits += 1; }
        }
        let started = Instant::now();
        let mut cancellation = Cancellation::new(cancelled);
        let mut aborted = false;
        loop {
            match self.device.poll(wgpu::PollType::Wait { submission_index: Some(index.clone()), timeout: Some(Duration::from_millis(50)) }) {
                Ok(_) => break,
                Err(wgpu::PollError::Timeout) => {
                    aborted |= cancellation.check().is_err();
                    if started.elapsed() >= Duration::from_secs(30) {
                        self.lost.store(true, Ordering::Release);
                        self.device.destroy();
                        return Err(error("device_lost", "GPU submission timed out"));
                    }
                }
                Err(reason) => {
                    self.check()?;
                    return Err(error("execution_failed", reason));
                }
            }
        }
        let event = self.completed.fetch_add(1, Ordering::AcqRel) + 1;
        let pending = std::mem::take(&mut *self.pending_kernels.lock().unwrap());
        let elapsed = pending.first().map_or(started.elapsed(), |(_, start)| start.elapsed()).as_secs_f64()*1000.0;
        let mut first = self.first_submission_ms.lock().unwrap();
        for (name, _) in pending { first.entry(name).or_insert(elapsed); }
        drop(first);
        self.check()?;
        if aborted { return Err(pyo3::exceptions::PyInterruptedError::new_err("cancelled")); }
        cancellation.check()?;
        Ok(event)
    }

    fn group(&self, name: &str, buffers: &[&wgpu::Buffer]) -> wgpu::BindGroup {
        let entries: Vec<_> = buffers.iter().enumerate().map(|(index, buffer)| wgpu::BindGroupEntry {
            binding: index as u32, resource: buffer.as_entire_binding(),
        }).collect();
        self.device.create_bind_group(&wgpu::BindGroupDescriptor { label: Some(name),
            layout: &self.pipelines[name].get_bind_group_layout(0), entries: &entries })
    }

    fn dispatch(&self, encoder: &mut wgpu::CommandEncoder, name: &str, buffers: &[&wgpu::Buffer], groups: (u32, u32)) {
        self.dispatch_named(encoder, name, name, buffers, groups);
    }

    fn dispatch_named(&self, encoder: &mut wgpu::CommandEncoder, name: &str, label: &str,
                      buffers: &[&wgpu::Buffer], groups: (u32, u32)) {
        if !self.first_submission_ms.lock().unwrap().contains_key(name) {
            let mut pending = self.pending_kernels.lock().unwrap();
            if !pending.iter().any(|(item, _)| item == name) { pending.push((name.to_owned(), Instant::now())); }
        }
        let group = self.group(name, buffers);
        let mut timing = self.timing.lock().unwrap();
        let timestamp_writes = timing.as_mut().and_then(|timing| timing.reserve(label));
        let mut pass = encoder.begin_compute_pass(&wgpu::ComputePassDescriptor { label: Some(name), timestamp_writes });
        pass.set_pipeline(&self.pipelines[name]);
        pass.set_bind_group(0, &group, &[]);
        pass.dispatch_workgroups(groups.0, groups.1, 1);
    }

    fn read(&self, io: &mut Io, source: &wgpu::Buffer, size: u64) -> PyResult<wgpu::Buffer> {
        if io.size < size {
            let mut pool = self.pool.lock().unwrap();
            let additional = size - io.size;
            if pool.total() + additional > pool.limit { pool.idle.clear(); }
            if pool.total() + additional > pool.limit { return Err(error("memory_pressure", "GPU readback exceeds budget")); }
            io.staging = None;
            pool.staging = 0;
            io.size = 0;
            let staging = self.device.create_buffer(&wgpu::BufferDescriptor { label: Some("ops-readback"), size,
                usage: wgpu::BufferUsages::COPY_DST | wgpu::BufferUsages::MAP_READ, mapped_at_creation: false });
            self.check()?;
            io.staging = Some(staging);
            io.size = size;
            pool.staging = size;
        }
        let staging = io.staging.as_ref().unwrap().clone();
        self.encoder(io)?.copy_buffer_to_buffer(source, 0, &staging, 0, size);
        Ok(staging)
    }

    fn mapped(&self, io: &mut Io, staging: &wgpu::Buffer, size: u64,
              cancelled: Option<Py<PyAny>>) -> PyResult<Vec<u8>> {
        let started = Instant::now();
        let (index, ticket) = self.launch(io, cancelled)?;
        let index = index.ok_or_else(|| error("readback_failed", "GPU readback was not recorded"))?;
        let (sender, receiver) = std::sync::mpsc::channel();
        staging.slice(..size).map_async(wgpu::MapMode::Read, move |result| { let _ = sender.send(result); });
        let waited = self.settle(index, ticket, started);
        if let Err(reason) = waited { staging.unmap(); return Err(reason); }
        receiver.recv().map_err(|reason| error("readback_failed", reason))?
            .map_err(|reason| error("readback_failed", reason))?;
        let output = staging.slice(..size).get_mapped_range().to_vec();
        staging.unmap();
        self.downloaded.fetch_add(size, Ordering::Relaxed);
        Ok(output)
    }
}

#[pyclass]
pub struct GpuDevice { state: Arc<State>, index: usize, compiler_probe: HashMap<&'static str, f64>, compiler_reason: String }

impl GpuDevice {
    fn compile(py: Python<'_>, index: usize, expected_luid: &str, generation: u64, budget: u64, compiler: wgpu::Dx12Compiler) -> PyResult<Self> {
        py.allow_threads(|| {
            let shader_compiler = if matches!(&compiler, wgpu::Dx12Compiler::StaticDxc) { "static-dxc" } else { "fxc" };
            let adapter = instance(compiler).enumerate_adapters(wgpu::Backends::DX12).into_iter().nth(index)
                .ok_or_else(|| error("adapter_missing", "GPU adapter is unavailable"))?;
            if !hardware(&adapter) || dxgi(&adapter).0 != expected_luid { return Err(error("adapter_changed", "GPU adapter identity changed")); }
            let limits = adapter.limits();
            let (device, queue) = pollster::block_on(adapter.request_device(&wgpu::DeviceDescriptor {
                label: Some("OPS"), required_features: adapter.features() & wgpu::Features::TIMESTAMP_QUERY,
                required_limits: limits.clone(),
                ..Default::default()
            })).map_err(|reason| error("device_creation", reason))?;
            let lost = Arc::new(AtomicBool::new(false));
            let failure = Arc::new(Mutex::new(None));
            let failed = failure.clone();
            device.on_uncaptured_error(Arc::new(move |reason: wgpu::Error| {
                let code = if matches!(reason, wgpu::Error::OutOfMemory { .. }) { "memory_pressure" } else { "execution_failed" };
                let mut pending = failed.lock().unwrap();
                if pending.is_none() { *pending = Some((code.to_owned(), reason.to_string())); }
            }));
            let failed = lost.clone();
            device.set_device_lost_callback(move |_, _| { failed.store(true, Ordering::Release); });
            device.push_error_scope(wgpu::ErrorFilter::Internal);
            device.push_error_scope(wgpu::ErrorFilter::Validation);
            let mut pipelines = HashMap::new();
            let mut compilation_ms = HashMap::new();
            let mut optional_failures = HashMap::new();
            for (name, text) in SHADERS {
                let optional = matches!(*name, "source_blocks" | "source_input" | "png_restore");
                if optional {
                    device.push_error_scope(wgpu::ErrorFilter::Internal);
                    device.push_error_scope(wgpu::ErrorFilter::Validation);
                }
                let started = Instant::now();
                let module = device.create_shader_module(wgpu::ShaderModuleDescriptor {
                    label: Some(name), source: wgpu::ShaderSource::Wgsl((*text).into()),
                });
                let pipeline = device.create_compute_pipeline(&wgpu::ComputePipelineDescriptor { label: Some(name), layout: None,
                    module: &module, entry_point: Some("main"), compilation_options: Default::default(), cache: None });
                if optional {
                    let validation = pollster::block_on(device.pop_error_scope());
                    let internal = pollster::block_on(device.pop_error_scope());
                    if let Some(reason) = validation.or(internal) {
                        optional_failures.insert(*name, reason.to_string());
                        continue;
                    }
                }
                pipelines.insert(*name, pipeline);
                compilation_ms.insert(*name, started.elapsed().as_secs_f64()*1000.0);
            }
            let validation = pollster::block_on(device.pop_error_scope());
            let internal = pollster::block_on(device.pop_error_scope());
            if let Some(reason) = validation.or(internal) { return Err(error("shader_compilation", reason)); }
            let compiler_probe = if shader_compiler == "fxc" { compilation_ms.clone() } else { HashMap::new() };
            let state = Arc::new(State { adapter, device, queue, limits, pipelines, compilation_ms, optional_failures, shader_compiler,
                first_submission_ms: Mutex::new(HashMap::new()), pending_kernels: Mutex::new(Vec::new()),
                timing: Mutex::new(None), io: Mutex::new(Io::default()),
                pool: Mutex::new(Pool { limit: budget, idle_limit: budget / 8, ..Default::default() }), lost, failure,
                completed: AtomicU64::new(0), submission_ms: AtomicU64::new(0),
                uploaded: AtomicU64::new(0), downloaded: AtomicU64::new(0), generation });
            state.check()?;
            Ok(Self { state, index, compiler_probe, compiler_reason: String::new() })
        })
    }
}

#[pymethods]
impl GpuDevice {
    fn start_timing(&self) -> PyResult<()> { self.begin_timing() }

    fn finish_timing<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> { self.end_timing(py) }

    #[staticmethod]
    fn color_transfer_lut(inverse: bool) -> Vec<f32> {
        if inverse { crate::color_space::get_srgb_to_lin_lut().to_vec() }
        else { crate::color_space::get_lin_to_srgb_lut().to_vec() }
    }

    #[new]
    #[pyo3(signature = (index, expected_luid, generation, budget=2147483648))]
    fn new(py: Python<'_>, index: usize, expected_luid: &str, generation: u64, budget: u64) -> PyResult<Self> {
        #[cfg(feature = "static-dxc")]
        let compiler = wgpu::Dx12Compiler::StaticDxc;
        #[cfg(not(feature = "static-dxc"))]
        let compiler = wgpu::Dx12Compiler::Fxc;
        Self::compile(py, index, expected_luid, generation, budget, compiler)
    }

    fn info<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let record = adapter_record(py, self.index, &self.state.adapter)?;
        let pool = self.state.pool.lock().unwrap();
        record.set_item("active_bytes", pool.active)?;
        record.set_item("idle_bytes", pool.idle_bytes())?;
        record.set_item("staging_bytes", pool.staging)?;
        record.set_item("transient_bytes", pool.transient)?;
        record.set_item("budget_bytes", pool.limit)?;
        record.set_item("allocated_bytes", pool.total())?;
        record.set_item("uploaded_bytes", self.state.uploaded.load(Ordering::Relaxed))?;
        record.set_item("readback_bytes", self.state.downloaded.load(Ordering::Relaxed))?;
        record.set_item("completed_event", self.state.completed.load(Ordering::Acquire))?;
        record.set_item("source_kernel_failures", &self.state.optional_failures)?;
        record.set_item("generation", self.state.generation)?;
        record.set_item("revision", REVISION)?;
        record.set_item("lost", self.state.lost.load(Ordering::Acquire))?;
        record.set_item("max_buffer_size", self.state.limits.max_buffer_size)?;
        record.set_item("max_binding_size", self.state.limits.max_storage_buffer_binding_size)?;
        record.set_item("max_workgroups", self.state.limits.max_compute_workgroups_per_dimension)?;
        record.set_item("kernel_compilation_ms", self.state.compilation_ms.clone())?;
        record.set_item("shader_compiler", self.state.shader_compiler)?;
        record.set_item("fxc_compilation_ms", self.compiler_probe.clone())?;
        record.set_item("compiler_reason", self.compiler_reason.clone())?;
        record.set_item("kernel_first_submission_ms", self.state.first_submission_ms.lock().unwrap().clone())?;
        Ok(record)
    }

    #[pyo3(signature = (budget, idle_limit, submission_ms=0))]
    fn configure(&self, budget: u64, idle_limit: u64, submission_ms: u64) {
        self.state.submission_ms.store(submission_ms, Ordering::Relaxed);
        let mut pool = self.state.pool.lock().unwrap();
        pool.limit = budget;
        pool.idle_limit = idle_limit.min(budget);
        while !pool.idle.is_empty() && (pool.idle_bytes() > pool.idle_limit || pool.total() > budget) { pool.idle.pop(); }
    }

    #[pyo3(signature = (cancelled=None))]
    fn flush(&self, py: Python<'_>, cancelled: Option<Py<PyAny>>) -> PyResult<u64> {
        py.allow_threads(|| {
            let mut io = self.state.io.lock().unwrap();
            self.state.flush(&mut io, cancelled)
        })
    }

    fn discard(&self, py: Python<'_>) {
        py.allow_threads(|| {
            let mut io = self.state.io.lock().unwrap();
            if io.owner == Some(std::thread::current().id()) { self.state.discard(&mut io); }
        });
    }

    fn usage(&self) -> (u64, u64) {
        (self.state.pool.lock().unwrap().total(), self.state.completed.load(Ordering::Acquire))
    }

    fn trim(&self, py: Python<'_>) {
        py.allow_threads(|| {
            let mut io = self.state.io.lock().unwrap();
            io.staging = None;
            io.size = 0;
            let mut pool = self.state.pool.lock().unwrap();
            pool.idle.clear();
            pool.staging = 0;
            drop(pool);
            let _ = self.state.device.poll(wgpu::PollType::Poll);
        });
    }

    fn close(&self, py: Python<'_>) {
        py.allow_threads(|| {
            let mut io = self.state.io.lock().unwrap();
            self.state.discard(&mut io);
            self.state.lost.store(true, Ordering::Release);
            self.state.pool.lock().unwrap().idle.clear();
            io.staging = None;
            io.size = 0;
            self.state.pool.lock().unwrap().staging = 0;
            self.state.device.destroy();
        });
    }

    #[pyo3(signature = (data, width, height, channels, depth, filtered, big_endian, lut,
                        output_width, output_height, matrix=None, cancelled=None, blocks=None))]
    fn source_image(&self, py: Python<'_>, data: numpy::PyReadonlyArray1<'_, u8>, width: usize, height: usize,
                    channels: usize, depth: usize, filtered: bool, big_endian: bool,
                    lut: numpy::PyReadonlyArray1<'_, f32>, output_width: usize, output_height: usize,
                    matrix: Option<Vec<f32>>, cancelled: Option<Py<PyAny>>, blocks: Option<PyReadonlyArray2<'_, u32>>) -> PyResult<GpuImage> {
        self.prepare_source_image(py, data, width, height, channels, depth, filtered, big_endian,
            lut, output_width, output_height, matrix, cancelled, blocks)
    }

    #[pyo3(signature = (data, blocks, expected, cancelled=None))]
    fn source_blocks_probe<'py>(&self, py: Python<'py>, data: numpy::PyReadonlyArray1<'_, u8>,
        blocks: PyReadonlyArray2<'_, u32>, expected: usize, cancelled: Option<Py<PyAny>>) -> PyResult<Bound<'py, numpy::PyArray1<u8>>> {
        if blocks.shape()[1] != 4 { return Err(error("source_blocks", "Expected Nx4 block index")); }
        let (data, index) = (data.as_slice()?, blocks.as_slice()?);
        let output = py.allow_threads(|| {
            let clone_cancel = || cancelled.as_ref().map(|value| Python::with_gil(|py| value.clone_ref(py)));
            let mut io = self.state.io.lock().unwrap();
            let buffer = self.state.source_blocks(&mut io, data, index, expected, clone_cancel())?;
            let size = expected.next_multiple_of(4) as u64;
            let staging = self.state.read(&mut io, buffer.buffer.as_ref().unwrap(), size)?;
            let mut bytes = self.state.mapped(&mut io, &staging, size, clone_cancel())?;
            bytes.truncate(expected);
            Ok::<_, PyErr>(bytes)
        })?;
        Ok(PyArray1::from_vec_bound(py, output))
    }

    #[pyo3(signature = (pixels, alpha=None, cancelled=None))]
    fn upload(&self, py: Python<'_>, pixels: PyReadonlyArray3<'_, f32>, alpha: Option<PyReadonlyArray2<'_, f32>>,
              cancelled: Option<Py<PyAny>>) -> PyResult<GpuImage> {
        let (height, width) = (pixels.shape()[0], pixels.shape()[1]);
        if width == 0 || height == 0 || pixels.shape()[2] != 3 || alpha.as_ref().is_some_and(|a| a.shape() != [height, width]) {
            return Err(pyo3::exceptions::PyValueError::new_err("expected RGB and matching Alpha"));
        }
        let rgb = pixels.as_array();
        let coverage = alpha.as_ref().map(|a| a.as_array());
        py.allow_threads(|| {
            let mut check = Cancellation::new(cancelled.as_ref().map(|value| Python::with_gil(|py| value.clone_ref(py))));
            check.check()?;
            let mut data = Vec::with_capacity(width * height * 4);
            for y in 0..height {
                if y % 64 == 0 { check.check()?; }
                for x in 0..width { data.extend_from_slice(&[rgb[[y, x, 0]], rgb[[y, x, 1]], rgb[[y, x, 2]],
                    coverage.as_ref().map_or(1.0, |a| a[[y, x]])]); }
            }
            self.state.upload_data(&data, width, height, cancelled)
        })
    }

    #[pyo3(signature = (pixels, cancelled=None))]
    fn upload_packed(&self, py: Python<'_>, pixels: PyReadonlyArray3<'_, f32>, cancelled: Option<Py<PyAny>>) -> PyResult<GpuImage> {
        let (height, width) = (pixels.shape()[0], pixels.shape()[1]);
        if width == 0 || height == 0 || pixels.shape()[2] != 4 {
            return Err(pyo3::exceptions::PyValueError::new_err("expected packed RGBA float32"));
        }
        let data = pixels.as_slice()?;
        py.allow_threads(|| self.state.upload_data(data, width, height, cancelled))
    }

    #[pyo3(signature = (image, width, height, cancelled=None))]
    fn display<'py>(&self, py: Python<'py>, image: &GpuImage, width: usize, height: usize,
                   cancelled: Option<Py<PyAny>>) -> PyResult<Bound<'py, PyArray3<u8>>> {
        if width == 0 || height == 0 || width > image.width || height > image.height || image.generation != self.state.generation
            || !Arc::ptr_eq(&image.storage.state, &self.state) {
            return Err(error("target_limit", "GPU image or display target is incompatible"));
        }
        if (image.width.div_ceil(width) + 1) * (image.height.div_ceil(height) + 1) > 4096 {
            return Err(error("target_limit", "GPU display footprint exceeds submission limit"));
        }
        let output = py.allow_threads(|| {
            let mut io = self.state.io.lock().unwrap();
            self.state.check()?;
            let size = (width * height * 4) as u64;
            let target = self.state.allocate(size)?;
            let uniform = self.state.uniform("ops-display-spec", bytemuck::cast_slice(&[image.width as u32, image.height as u32, width as u32, height as u32,
                    image.stride as u32, image.offset as u32, 0, 0]))?;
            self.state.dispatch(self.state.encoder(&mut io)?, "display_rgba", &[image.storage.buffer.as_ref().unwrap(), target.buffer.as_ref().unwrap(), &uniform],
                ((width as u32).div_ceil(8), (height as u32).div_ceil(8)));
            let staging = self.state.read(&mut io, target.buffer.as_ref().unwrap(), size)?;
            self.state.mapped(&mut io, &staging, size, cancelled)
        })?;
        PyArray1::from_vec_bound(py, output).reshape([height, width, 4])
    }

    #[pyo3(signature = (threads=8, cpu_admission=None))]
    fn qualify<'py>(&self, py: Python<'py>, threads: usize, cpu_admission: Option<Py<PyAny>>) -> PyResult<Bound<'py, PyDict>> {
        let result = py.allow_threads(|| self.state.qualify(threads, cpu_admission.as_ref()))?;
        let report = PyDict::new_bound(py);
        report.set_item("max_lsb", result.0)?;
        report.set_item("cpu_ms", result.1)?;
        report.set_item("gpu_ms", result.2)?;
        report.set_item("warmup_ms", result.3)?;
        report.set_item("passed", result.0 <= 1 && result.2 < result.1 * 0.8)?;
        Ok(report)
    }

    fn view(&self, image: &GpuImage, left: usize, top: usize, width: usize, height: usize) -> PyResult<GpuImage> {
        self.view_image(image, left, top, width, height)
    }

    #[pyo3(signature = (image, guide, radius, eps, subsample=1, cancelled=None))]
    fn guided(&self, py: Python<'_>, image: &GpuImage, guide: &GpuImage, radius: u32, eps: f32,
              subsample: usize, cancelled: Option<Py<PyAny>>) -> PyResult<GpuImage> {
        self.guided_image(py, image, guide, radius, eps, subsample, cancelled)
    }

    #[pyo3(signature = (width, height, primitives, cancelled=None))]
    fn shape_mask(&self, py: Python<'_>, width: usize, height: usize, primitives: Vec<Vec<i32>>,
                  cancelled: Option<Py<PyAny>>) -> PyResult<GpuImage> {
        self.raster_shape(py, width, height, primitives, cancelled)
    }

    #[pyo3(signature = (width, height, regions, invert=false, feather=0.0, cancelled=None))]
    fn lasso_mask(&self, py: Python<'_>, width: usize, height: usize,
                  regions: Vec<(bool, Vec<(i32,i32)>)>, invert: bool, feather: f32,
                  cancelled: Option<Py<PyAny>>) -> PyResult<GpuImage> {
        self.raster_lasso(py, width, height, regions, invert, feather, cancelled)
    }

    #[pyo3(signature = (image, other, mode, args, width, height, cancelled=None))]
    fn image_op(&self, py: Python<'_>, image: &GpuImage, other: &GpuImage, mode: u32, args: Vec<f32>,
                width: usize, height: usize, cancelled: Option<Py<PyAny>>) -> PyResult<GpuImage> {
        self.run_image(py, image, other, mode, args, width, height, cancelled)
    }

    #[pyo3(signature = (image, auxiliary, field, extra, mode, args, data, cancelled=None))]
    fn color_op(&self, py: Python<'_>, image: &GpuImage, auxiliary: &GpuImage, field: &GpuImage,
        extra: &GpuImage, mode: u32, args: Vec<f32>, data: Vec<f32>, cancelled: Option<Py<PyAny>>) -> PyResult<GpuImage> {
        self.color_image(py, image, auxiliary, field, extra, mode, args, data, cancelled)
    }

    #[pyo3(signature = (image, cancelled=None))]
    fn nearest_visible(&self, py: Python<'_>, image: &GpuImage, cancelled: Option<Py<PyAny>>) -> PyResult<GpuImage> {
        self.nearest_visible_image(py, image, cancelled)
    }

    #[pyo3(signature = (images, positions, width, height, cancelled=None))]
    fn assemble(&self, py: Python<'_>, images: Vec<PyRef<'_, GpuImage>>, positions: Vec<(usize, usize)>,
        width: usize, height: usize, cancelled: Option<Py<PyAny>>) -> PyResult<GpuImage> {
        self.assemble_images(py, images, positions, width, height, cancelled)
    }

    #[pyo3(signature = (image, auxiliary, field, extra, kernel, mode, args, data, width, height, cancelled=None))]
    fn beauty_op(&self, py: Python<'_>, image: &GpuImage, auxiliary: &GpuImage, field: &GpuImage,
        extra: &GpuImage, kernel: &str, mode: u32, args: Vec<f32>, data: Vec<f32>, width: usize, height: usize,
        cancelled: Option<Py<PyAny>>) -> PyResult<GpuImage> {
        self.beauty_image(py, image, auxiliary, field, extra, kernel, mode, args, data, width, height, cancelled)
    }

    #[pyo3(signature = (image, cancelled=None, rows=false))]
    fn beauty_median(&self, py: Python<'_>, image: &GpuImage, cancelled: Option<Py<PyAny>>, rows: bool) -> PyResult<GpuImage> {
        self.median_image(py, image, rows, cancelled)
    }

    #[pyo3(signature = (image, roots, distance, cancelled=None))]
    fn beauty_components(&self, py: Python<'_>, image: &GpuImage, roots: &GpuImage, distance: f32,
                         cancelled: Option<Py<PyAny>>) -> PyResult<GpuImage> {
        self.components_image(py, image, roots, distance, cancelled)
    }

    #[pyo3(signature = (image, left, top, width, height, cancelled=None))]
    fn read_region<'py>(&self, py: Python<'py>, image: &GpuImage, left: usize, top: usize, width: usize,
                        height: usize, cancelled: Option<Py<PyAny>>) -> PyResult<Bound<'py, PyArray3<f32>>> {
        self.read_image(py, image, left, top, width, height, cancelled)
    }

    #[cfg(windows)]
    #[pyo3(signature = (directml_path=None))]
    fn interop_probe<'py>(&self, py: Python<'py>, directml_path: Option<&str>) -> PyResult<Bound<'py, PyDict>> {
        self.interop_report(py, directml_path)
    }

    #[cfg(windows)]
    #[pyo3(signature = (runtime_path, directml_path, model_path, input_size, output_size, channels=3, nchw=true, input_srgb=false, output_srgb=false, scale=1.0, bias=0.0))]
    fn create_model(&self, py: Python<'_>, runtime_path: &str, directml_path: &str, model_path: &str,
        input_size: (usize,usize), output_size: (usize,usize), channels: u32, nchw: bool,
        input_srgb: bool, output_srgb: bool, scale: f32, bias: f32) -> PyResult<GpuModel> {
        self.ort_model(py, runtime_path, directml_path, model_path, input_size, output_size,
            channels, nchw, input_srgb, output_srgb, scale, bias, None)
    }

    #[cfg(windows)]
    #[pyo3(signature = (runtime_path, directml_path, model_path, input_size, output_shapes, channels=3, nchw=false, input_srgb=false, scale=1.0, bias=0.0))]
    fn create_tensor_model(&self, py: Python<'_>, runtime_path: &str, directml_path: &str, model_path: &str,
        input_size: (usize,usize), output_shapes: Vec<Vec<usize>>, channels: u32, nchw: bool,
        input_srgb: bool, scale: f32, bias: f32) -> PyResult<GpuModel> {
        self.ort_model(py, runtime_path, directml_path, model_path, input_size, (1,1),
            channels, nchw, input_srgb, false, scale, bias, Some(output_shapes))
    }

    #[cfg(windows)]
    #[pyo3(signature = (image, auxiliary, mode, args, data, width, height, tensor_a=None, cancelled=None))]
    fn face_op(&self,py:Python<'_>,image:&GpuImage,auxiliary:&GpuImage,mode:u32,args:Vec<f32>,data:Vec<f32>,width:usize,height:usize,
        tensor_a:Option<&GpuTensor>,cancelled:Option<Py<PyAny>>)->PyResult<GpuImage> {
        self.face_image(py,image,auxiliary,tensor_a,mode,args,data,width,height,cancelled)
    }

    #[cfg(windows)]
    #[pyo3(signature = (tensors, cancelled=None))]
    fn pack_tensors(&self,py:Python<'_>,tensors:Vec<PyRef<'_,GpuTensor>>,cancelled:Option<Py<PyAny>>)->PyResult<GpuTensor> {
        self.join_tensors(py,tensors,cancelled)
    }
}
