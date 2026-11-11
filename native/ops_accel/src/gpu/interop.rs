use super::*;
use std::{ffi::c_void, mem::ManuallyDrop};
use windows::{core::{Interface, GUID, HRESULT, PCSTR, PCWSTR, w}, Win32::{
    AI::MachineLearning::DirectML::*, Foundation::{FreeLibrary, HMODULE},
    Graphics::Direct3D12::*, System::LibraryLoader::{LoadLibraryExW, GetProcAddress, LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR, LOAD_LIBRARY_SEARCH_SYSTEM32},
}};

struct Library(HMODULE);
impl Drop for Library { fn drop(&mut self) { unsafe { let _ = FreeLibrary(self.0); } } }

struct Binding(DML_BUFFER_BINDING);
impl Binding {
    fn new(resource: &ID3D12Resource, size: u64) -> Self {
        Self(DML_BUFFER_BINDING { Buffer: ManuallyDrop::new(Some(resource.clone())), Offset: 0, SizeInBytes: size })
    }
    fn desc(&self) -> DML_BINDING_DESC {
        DML_BINDING_DESC { Type: DML_BINDING_TYPE_BUFFER, Desc: &self.0 as *const _ as *const c_void }
    }
}
impl Drop for Binding { fn drop(&mut self) { unsafe { ManuallyDrop::drop(&mut self.0.Buffer); } } }

pub(super) unsafe fn raw_buffer(device: &ID3D12Device, size: u64) -> windows::core::Result<ID3D12Resource> {
    let mut result = None;
    device.CreateCommittedResource(&D3D12_HEAP_PROPERTIES { Type: D3D12_HEAP_TYPE_DEFAULT, ..Default::default() },
        D3D12_HEAP_FLAG_NONE, &D3D12_RESOURCE_DESC { Dimension: D3D12_RESOURCE_DIMENSION_BUFFER,
            Width: size.max(4), Height: 1, DepthOrArraySize: 1, MipLevels: 1,
            SampleDesc: windows::Win32::Graphics::Dxgi::Common::DXGI_SAMPLE_DESC { Count: 1, Quality: 0 },
            Layout: D3D12_TEXTURE_LAYOUT_ROW_MAJOR, Flags: D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS, ..Default::default() },
        D3D12_RESOURCE_STATE_COMMON, None, &mut result)?;
    result.ok_or_else(windows::core::Error::from_win32)
}

unsafe fn binding_table(device: &IDMLDevice, operation: &IDMLDispatchable, heap: &ID3D12DescriptorHeap,
                        offset: usize, count: u32, increment: u32) -> windows::core::Result<IDMLBindingTable> {
    let mut cpu = heap.GetCPUDescriptorHandleForHeapStart();
    let mut gpu = heap.GetGPUDescriptorHandleForHeapStart();
    cpu.ptr += offset * increment as usize;
    gpu.ptr += (offset * increment as usize) as u64;
    let mut desc = DML_BINDING_TABLE_DESC { Dispatchable: ManuallyDrop::new(Some(operation.clone())),
        CPUDescriptorHandle: cpu, GPUDescriptorHandle: gpu, SizeInDescriptors: count };
    let result = device.CreateBindingTable(Some(&desc));
    ManuallyDrop::drop(&mut desc.Dispatchable);
    result
}

unsafe fn barrier(list: &ID3D12GraphicsCommandList) {
    list.ResourceBarrier(&[D3D12_RESOURCE_BARRIER { Type: D3D12_RESOURCE_BARRIER_TYPE_UAV,
        Flags: D3D12_RESOURCE_BARRIER_FLAG_NONE, Anonymous: D3D12_RESOURCE_BARRIER_0 {
            UAV: ManuallyDrop::new(D3D12_RESOURCE_UAV_BARRIER { pResource: ManuallyDrop::new(None) }) } }]);
}

unsafe fn make_writable(list: &ID3D12GraphicsCommandList, resource: &ID3D12Resource) {
    let mut transition = D3D12_RESOURCE_BARRIER { Type: D3D12_RESOURCE_BARRIER_TYPE_TRANSITION,
        Flags: D3D12_RESOURCE_BARRIER_FLAG_NONE, Anonymous: D3D12_RESOURCE_BARRIER_0 {
            Transition: ManuallyDrop::new(D3D12_RESOURCE_TRANSITION_BARRIER {
                pResource: ManuallyDrop::new(Some(resource.clone())), Subresource: D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES,
                StateBefore: D3D12_RESOURCE_STATE_COMMON, StateAfter: D3D12_RESOURCE_STATE_UNORDERED_ACCESS,
            }) } };
    list.ResourceBarrier(std::slice::from_ref(&transition));
    ManuallyDrop::drop(&mut (*transition.Anonymous.Transition).pResource);
}

impl State {
    unsafe fn inference_probe(self: &Arc<Self>, io: &mut Io, path: Option<&str>) -> PyResult<(u8, u64)> {
        let _library = if let Some(path) = path {
            if !std::path::Path::new(path).is_absolute() { return Err(error("interop_library", "DirectML path must be absolute")); }
            let wide: Vec<_> = path.encode_utf16().chain(Some(0)).collect();
            Library(LoadLibraryExW(PCWSTR(wide.as_ptr()), None, LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_SYSTEM32)
                .map_err(|reason| error("interop_library", reason))?)
        } else {
            Library(LoadLibraryExW(w!("DirectML.dll"), None, LOAD_LIBRARY_SEARCH_SYSTEM32)
                .map_err(|reason| error("interop_library", reason))?)
        };
        let create = GetProcAddress(_library.0, PCSTR(b"DMLCreateDevice\0".as_ptr()))
            .ok_or_else(|| error("interop_library", "DMLCreateDevice is unavailable"))?;
        let create: unsafe extern "system" fn(*mut c_void, DML_CREATE_DEVICE_FLAGS, *const GUID, *mut *mut c_void) -> HRESULT = std::mem::transmute(create);
        let hal = self.device.as_hal::<wgpu::hal::api::Dx12>().ok_or_else(|| error("interop_backend", "D3D12 is required"))?;
        let raw = hal.raw_device().clone();
        drop(hal);
        let queue = self.queue.as_hal::<wgpu::hal::api::Dx12>().ok_or_else(|| error("interop_backend", "D3D12 queue is unavailable"))?.as_raw().clone();
        let mut pointer = std::ptr::null_mut();
        create(raw.as_raw(), DML_CREATE_DEVICE_FLAG_NONE, &IDMLDevice::IID, &mut pointer)
            .ok().map_err(|reason| error("interop_device", reason))?;
        let dml = IDMLDevice::from_raw(pointer);
        let sizes = [1u32, 64, 64, 4];
        let tensor_buffer = DML_BUFFER_TENSOR_DESC { DataType: DML_TENSOR_DATA_TYPE_FLOAT32, Flags: DML_TENSOR_FLAG_NONE,
            DimensionCount: 4, Sizes: sizes.as_ptr(), Strides: std::ptr::null(), TotalTensorSizeInBytes: 64 * 64 * 16,
            GuaranteedBaseOffsetAlignment: 0 };
        let tensor = DML_TENSOR_DESC { Type: DML_TENSOR_TYPE_BUFFER, Desc: &tensor_buffer as *const _ as *const c_void };
        let relu = DML_ACTIVATION_RELU_OPERATOR_DESC { InputTensor: &tensor, OutputTensor: &tensor };
        let mut operation: Option<IDMLOperator> = None;
        dml.CreateOperator(&DML_OPERATOR_DESC { Type: DML_OPERATOR_ACTIVATION_RELU, Desc: &relu as *const _ as *const c_void }, &mut operation)
            .map_err(|reason| error("interop_operator", reason))?;
        let mut compiled: Option<IDMLCompiledOperator> = None;
        dml.CompileOperator(operation.as_ref().unwrap(), DML_EXECUTION_FLAG_NONE, &mut compiled)
            .map_err(|reason| error("interop_operator", reason))?;
        let compiled = compiled.unwrap();
        let initializer: IDMLOperatorInitializer = dml.CreateOperatorInitializer(Some(&[Some(compiled.clone())]))
            .map_err(|reason| error("interop_initializer", reason))?;
        let init_props = initializer.GetBindingProperties();
        let props = compiled.GetBindingProperties();
        let temporary_size = props.TemporaryResourceSize.max(init_props.TemporaryResourceSize);
        let total_external = 2 * tensor_buffer.TotalTensorSizeInBytes + temporary_size.max(4) + props.PersistentResourceSize.max(4);
        {
            let mut pool = self.pool.lock().unwrap();
            pool.idle.clear();
            if pool.total() + total_external + 64 * 64 * 16 + 16 * 16 * 8 > pool.limit {
                return Err(error("memory_pressure", "DirectML prototype exceeds GPU budget"));
            }
            pool.transient = total_external;
        }
        let result = (|| -> PyResult<(u8, u64)> {
            let input = raw_buffer(&raw, tensor_buffer.TotalTensorSizeInBytes).map_err(|reason| error("interop_buffer", reason))?;
            let output = raw_buffer(&raw, tensor_buffer.TotalTensorSizeInBytes).map_err(|reason| error("interop_buffer", reason))?;
            let temporary = raw_buffer(&raw, temporary_size.max(4)).map_err(|reason| error("interop_buffer", reason))?;
            let persistent = raw_buffer(&raw, props.PersistentResourceSize.max(4)).map_err(|reason| error("interop_buffer", reason))?;
            let wrap = |resource: &ID3D12Resource| {
                let size = tensor_buffer.TotalTensorSizeInBytes;
                let buffer = wgpu::hal::dx12::Device::buffer_from_raw(resource.clone(), size);
                self.device.create_buffer_from_hal::<wgpu::hal::api::Dx12>(buffer, &wgpu::BufferDescriptor {
                    label: Some("ops-dml-tensor"), size, usage: STORAGE, mapped_at_creation: false,
                })
            };
            let input_buffer = wrap(&input);
            let output_buffer = wrap(&output);
            let source = self.allocate(64 * 64 * 16)?;
            let packed: Vec<f32> = (0..64 * 64 * 4).map(|index| (index % 257) as f32 / 256.0).collect();
            self.queue.write_buffer(source.buffer.as_ref().unwrap(), 0, bytemuck::cast_slice(&packed));
            let mut encoder = self.device.create_command_encoder(&Default::default());
            self.dispatch(&mut encoder, "interop_prepare", &[source.buffer.as_ref().unwrap(), &input_buffer, &output_buffer], (64, 1));
            self.wait(self.queue.submit([encoder.finish()]), None)?;
            let descriptors = props.RequiredDescriptorCount.max(init_props.RequiredDescriptorCount).max(1);
            let heap: ID3D12DescriptorHeap = raw.CreateDescriptorHeap(&D3D12_DESCRIPTOR_HEAP_DESC {
                Type: D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV, NumDescriptors: descriptors * 2,
                Flags: D3D12_DESCRIPTOR_HEAP_FLAG_SHADER_VISIBLE, NodeMask: 0,
            }).map_err(|reason| error("interop_binding", reason))?;
            let increment = raw.GetDescriptorHandleIncrementSize(D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV);
            let init = binding_table(&dml, &initializer.cast().unwrap(), &heap, 0, descriptors, increment)
                .map_err(|reason| error("interop_binding", reason))?;
            let execute = binding_table(&dml, &compiled.cast().unwrap(), &heap, descriptors as usize, descriptors, increment)
                .map_err(|reason| error("interop_binding", reason))?;
            let temporary_binding = Binding::new(&temporary, temporary_size);
            let persistent_binding = Binding::new(&persistent, props.PersistentResourceSize);
            let persistent_desc = if props.PersistentResourceSize > 0 { persistent_binding.desc() }
                else { DML_BINDING_DESC { Type: DML_BINDING_TYPE_NONE, Desc: std::ptr::null() } };
            if temporary_size > 0 {
                let desc = temporary_binding.desc();
                init.BindTemporaryResource(Some(&desc));
                execute.BindTemporaryResource(Some(&desc));
            }
            init.BindOutputs(Some(&[persistent_desc]));
            if props.PersistentResourceSize > 0 { execute.BindPersistentResource(Some(&persistent_binding.desc())); }
            let input_binding = Binding::new(&input, tensor_buffer.TotalTensorSizeInBytes);
            let output_binding = Binding::new(&output, tensor_buffer.TotalTensorSizeInBytes);
            execute.BindInputs(Some(&[input_binding.desc()]));
            execute.BindOutputs(Some(&[output_binding.desc()]));
            let allocator: ID3D12CommandAllocator = raw.CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT)
                .map_err(|reason| error("interop_commands", reason))?;
            let list: ID3D12GraphicsCommandList = raw.CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT, &allocator, None)
                .map_err(|reason| error("interop_commands", reason))?;
            list.SetDescriptorHeaps(&[Some(heap.clone())]);
            make_writable(&list, &temporary);
            make_writable(&list, &persistent);
            let recorder: IDMLCommandRecorder = dml.CreateCommandRecorder().map_err(|reason| error("interop_commands", reason))?;
            let command: ID3D12CommandList = list.cast().unwrap();
            recorder.RecordDispatch(&command, &initializer, &init);
            barrier(&list);
            recorder.RecordDispatch(&command, &compiled, &execute);
            barrier(&list);
            list.Close().map_err(|reason| error("interop_commands", reason))?;
            queue.ExecuteCommandLists(&[Some(command)]);
            self.wait(self.queue.submit([]), None)?;
            let target = self.allocate(16 * 16 * 4)?;
            let uniform = self.uniform("ops-interop-viewport", bytemuck::cast_slice(&[64u32, 64, 16, 16, 64, 0, 0, 0]))?;
            let mut encoder = self.device.create_command_encoder(&Default::default());
            self.dispatch(&mut encoder, "display_rgba", &[&output_buffer, target.buffer.as_ref().unwrap(), &uniform], (2, 2));
            let staging = self.read(io, &mut encoder, target.buffer.as_ref().unwrap(), 16 * 16 * 4)?;
            let pixels = self.mapped(encoder, &staging, 16 * 16 * 4, None)?;
            let mut full = Vec::new();
            for rgba in packed.chunks_exact(4) {
                full.extend_from_slice(&crate::display::pixel([rgba[0].max(0.25) - 0.25, rgba[1].max(0.25) - 0.25,
                    rgba[2].max(0.25) - 0.25], rgba[3].max(0.25) - 0.25));
            }
            let expected = crate::display_output::area(&full, 64, 64, 16, 16, &mut Cancellation::new(None))?;
            let max_lsb = pixels.iter().zip(expected).map(|(a, b)| a.abs_diff(b)).max().unwrap_or(255);
            Ok((max_lsb, total_external))
        })();
        self.pool.lock().unwrap().transient = 0;
        result
    }
}

impl GpuDevice {
    pub(super) fn interop_report<'py>(&self, py: Python<'py>, directml_path: Option<&str>) -> PyResult<Bound<'py, PyDict>> {
        let result = py.allow_threads(|| unsafe {
            let mut io = self.state.io.lock().unwrap();
            self.state.inference_probe(&mut io, directml_path)
        })?;
        let record = PyDict::new_bound(py);
        record.set_item("model", "DirectML-ReLU-f32")?;
        record.set_item("layout", "1x64x64x4-dense")?;
        record.set_item("device_luid", dxgi(&self.state.adapter).0)?;
        record.set_item("shared_device", true)?;
        record.set_item("shared_queue", true)?;
        record.set_item("intermediate_readback_bytes", 0)?;
        record.set_item("upload_bytes", 64 * 64 * 16)?;
        record.set_item("viewport_readback_bytes", 16 * 16 * 4)?;
        record.set_item("external_bytes", result.1)?;
        record.set_item("max_lsb", result.0)?;
        record.set_item("passed", result.0 <= 1)?;
        Ok(record)
    }
}
