use super::*;
use std::ffi::{c_void,CStr};
use windows::{core::{Interface,GUID,HRESULT,PCSTR,PCWSTR},Win32::{
    AI::MachineLearning::DirectML::{IDMLDevice,DML_CREATE_DEVICE_FLAG_NONE,DML_CREATE_DEVICE_FLAGS},
    Foundation::{HMODULE,FreeLibrary},Graphics::Direct3D12::ID3D12Resource,
    System::LibraryLoader::{LoadLibraryExW,GetProcAddress,LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR,LOAD_LIBRARY_SEARCH_SYSTEM32},
}};

extern "C" {
    fn ops_ort_run(model:usize,error:*mut i8,capacity:usize)->i32;
    fn ops_ort_close(model:usize);
    fn ops_ort_open_tensors(base:*const c_void,device:*mut c_void,queue:*mut c_void,path:*const u16,input:*mut c_void,
        outputs:*const *mut c_void,input_shape:*const i64,output_shapes:*const *const i64,output_ranks:*const usize,
        input_bytes:usize,output_bytes:*const usize,output_count:usize,result:*mut usize,error:*mut i8,capacity:usize)->i32;
}

struct Library(usize);
impl Drop for Library { fn drop(&mut self) { unsafe { let _=FreeLibrary(self.handle()); } } }
impl Library {
    fn handle(&self)->HMODULE { HMODULE(self.0 as *mut c_void) }
    unsafe fn load(path:&str)->PyResult<Self> {
        if !std::path::Path::new(path).is_absolute() { return Err(error("model_runtime","Runtime path must be absolute")); }
        let wide:Vec<_>=path.encode_utf16().chain(Some(0)).collect();
        Ok(Self(LoadLibraryExW(PCWSTR(wide.as_ptr()),None,LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR|LOAD_LIBRARY_SEARCH_SYSTEM32)
            .map_err(|reason|error("model_runtime",reason))?.0 as usize))
    }
}

struct Charge { state:Arc<State>,bytes:u64 }
impl Drop for Charge { fn drop(&mut self) { self.state.pool.lock().unwrap().active-=self.bytes; } }

struct Session {
    handle:usize,
    input:wgpu::Buffer,
    output:Vec<wgpu::Buffer>,
    _raw_input:ID3D12Resource,
    _raw_output:Vec<ID3D12Resource>,
    _charge:Charge,
    _runtime:Library,
    _directml:Library,
}
impl Drop for Session { fn drop(&mut self) { unsafe { ops_ort_close(self.handle); } } }

#[pyclass]
pub struct GpuModel {
    state:Arc<State>,
    session:Mutex<Option<Session>>,
    input_size:(usize,usize),
    output_size:(usize,usize),
    channels:u32,
    nchw:bool,
    input_srgb:bool,
    output_srgb:bool,
    scale:f32,
    bias:f32,
    tensor_shapes:Option<Vec<Vec<usize>>>,
}

#[pyclass(frozen)]
pub struct GpuTensor { pub(super) storage:Arc<Allocation>, pub(super) shape:Vec<usize>, pub(super) event:u64 }

#[pymethods]
impl GpuTensor {
    #[getter]
    fn shape(&self)->Vec<usize> { self.shape.clone() }
    #[getter]
    fn size(&self)->(usize,usize) { ((self.storage.size as usize).div_ceil(16),1) }
    #[getter]
    fn nbytes(&self)->u64 { self.storage.size }
    #[getter]
    fn allocation_id(&self)->usize { Arc::as_ptr(&self.storage) as usize }
    #[getter]
    fn completion(&self)->u64 { self.event }
    #[getter]
    fn generation(&self)->u64 { self.storage.state.generation }
}

impl GpuDevice {
    pub(super) fn ort_model(&self,py:Python<'_>,runtime_path:&str,directml_path:&str,model_path:&str,
        input_size:(usize,usize),output_size:(usize,usize),channels:u32,nchw:bool,
        input_srgb:bool,output_srgb:bool,scale:f32,bias:f32,tensor_shapes:Option<Vec<Vec<usize>>>)->PyResult<GpuModel> {
        if !(3..=4).contains(&channels) || !scale.is_finite() || !bias.is_finite()
            || [input_size.0,input_size.1,output_size.0,output_size.1].iter().any(|v|*v==0||*v>32768)
            || !std::path::Path::new(model_path).is_absolute() {
            return Err(error("model_layout","Unsupported GPU model specification"));
        }
        let input_bytes=(input_size.0*input_size.1*channels as usize*4) as u64;
        let shape=|size:(usize,usize)| if nchw { vec![1,channels as i64,size.1 as i64,size.0 as i64] }
            else { vec![1,size.1 as i64,size.0 as i64,channels as i64] };
        let shapes:Vec<Vec<i64>>=tensor_shapes.as_ref().map(|values|values.iter().map(|shape|shape.iter().map(|v|*v as i64).collect()).collect())
            .unwrap_or_else(||vec![shape(output_size)]);
        if shapes.is_empty()||shapes.len()>32||shapes.iter().any(|shape|shape.is_empty()||shape.len()>8||shape.iter().any(|v|*v<=0||*v>100_000_000)) {
            return Err(error("model_layout","Invalid GPU tensor shape"));
        }
        let output_bytes:Vec<u64>=shapes.iter().map(|shape|shape.iter().try_fold(4u64,|size,v|size.checked_mul(*v as u64))
            .unwrap_or(u64::MAX)).collect();
        if input_bytes>self.state.limits.max_storage_buffer_binding_size as u64
            ||output_bytes.iter().any(|bytes|*bytes>self.state.limits.max_storage_buffer_binding_size as u64) {
            return Err(error("target_limit","GPU model tensor exceeds adapter limits"));
        }
        let session=py.allow_threads(|| unsafe {
            let mut io=self.state.io.lock().unwrap(); self.state.flush(&mut io,None)?;
            let runtime=Library::load(runtime_path)?; let directml=Library::load(directml_path)?;
            let get_base=GetProcAddress(runtime.handle(),PCSTR(b"OrtGetApiBase\0".as_ptr())).ok_or_else(||error("model_runtime","OrtGetApiBase is unavailable"))?;
            let get_base:unsafe extern "system" fn()->*const c_void=std::mem::transmute(get_base);
            let create=GetProcAddress(directml.handle(),PCSTR(b"DMLCreateDevice\0".as_ptr())).ok_or_else(||error("model_runtime","DMLCreateDevice is unavailable"))?;
            let create:unsafe extern "system" fn(*mut c_void,DML_CREATE_DEVICE_FLAGS,*const GUID,*mut *mut c_void)->HRESULT=std::mem::transmute(create);
            let hal=self.state.device.as_hal::<wgpu::hal::api::Dx12>().ok_or_else(||error("model_runtime","D3D12 is required"))?;
            let raw=hal.raw_device().clone(); drop(hal);
            let queue=self.state.queue.as_hal::<wgpu::hal::api::Dx12>().unwrap().as_raw().clone();
            let mut pointer=std::ptr::null_mut();
            create(raw.as_raw(),DML_CREATE_DEVICE_FLAG_NONE,&IDMLDevice::IID,&mut pointer).ok().map_err(|reason|error("model_runtime",reason))?;
            let dml=IDMLDevice::from_raw(pointer);
            let bytes=input_bytes+output_bytes.iter().sum::<u64>();
            { let mut pool=self.state.pool.lock().unwrap(); pool.idle.clear();
                if pool.total()+bytes>pool.limit { return Err(error("memory_pressure","Model tensors exceed GPU budget")); }
                pool.active+=bytes;
            }
            let charge=Charge{state:self.state.clone(),bytes};
            let input=super::interop::raw_buffer(&raw,input_bytes).map_err(|reason|error("memory_pressure",reason))?;
            let output:Vec<_>=output_bytes.iter().map(|bytes|super::interop::raw_buffer(&raw,*bytes)
                .map_err(|reason|error("memory_pressure",reason))).collect::<PyResult<_>>()?;
            let wide:Vec<_>=model_path.encode_utf16().chain(Some(0)).collect(); let mut handle=0usize; let mut message=[0i8;4096];
            let pointers:Vec<_>=output.iter().map(|v|v.as_raw()).collect();
            let shape_pointers:Vec<_>=shapes.iter().map(|v|v.as_ptr()).collect();
            let ranks:Vec<_>=shapes.iter().map(|v|v.len()).collect();
            let sizes:Vec<_>=output_bytes.iter().map(|v|*v as usize).collect();
            let status=ops_ort_open_tensors(get_base(),dml.as_raw(),queue.as_raw(),wide.as_ptr(),input.as_raw(),pointers.as_ptr(),
                shape(input_size).as_ptr(),shape_pointers.as_ptr(),ranks.as_ptr(),input_bytes as usize,sizes.as_ptr(),output.len(),
                &mut handle,message.as_mut_ptr(),message.len());
            let waited=self.state.wait(self.state.queue.submit([]),None);
            if let Err(reason)=waited { if status==0 { ops_ort_close(handle); } return Err(reason); }
            if status!=0 { return Err(error("model_capability",CStr::from_ptr(message.as_ptr()).to_string_lossy())); }
            let wrap=|resource:&ID3D12Resource,size:u64|self.state.device.create_buffer_from_hal::<wgpu::hal::api::Dx12>(
                wgpu::hal::dx12::Device::buffer_from_raw(resource.clone(),size),&wgpu::BufferDescriptor{
                    label:Some("ops-ort-tensor"),size,usage:STORAGE,mapped_at_creation:false});
            let wrapped=output.iter().zip(&output_bytes).map(|(resource,size)|wrap(resource,*size)).collect();
            Ok(Session{handle,input:wrap(&input,input_bytes),output:wrapped,_raw_input:input,_raw_output:output,
                _charge:charge,_runtime:runtime,_directml:directml})
        })?;
        Ok(GpuModel{state:self.state.clone(),session:Mutex::new(Some(session)),input_size,output_size,channels,nchw,input_srgb,output_srgb,scale,bias,tensor_shapes})
    }
}

#[pymethods]
impl GpuModel {
    #[getter]
    fn generation(&self)->u64 { self.state.generation }
    #[getter]
    fn device_luid(&self)->String { dxgi(&self.state.adapter).0 }
    #[getter]
    fn tensor_bytes(&self)->u64 {
        (self.input_size.0*self.input_size.1*self.channels as usize*4) as u64 + self.tensor_shapes.as_ref()
            .map(|shapes|shapes.iter().map(|shape|shape.iter().product::<usize>() as u64*4).sum::<u64>())
            .unwrap_or((self.output_size.0*self.output_size.1*self.channels as usize*4) as u64)
    }

    #[pyo3(signature = (image, cancelled=None))]
    fn run(&self,py:Python<'_>,image:&GpuImage,cancelled:Option<Py<PyAny>>)->PyResult<GpuImage> {
        self.state.image_check(image)?;
        if self.tensor_shapes.is_some() { return Err(error("model_layout","Tensor models require run_tensors")); }
        if (image.width,image.height)!=self.input_size { return Err(error("model_layout","GPU input size mismatch")); }
        py.allow_threads(|| unsafe {
            let mut io=self.state.io.lock().unwrap();
            let session=self.session.lock().unwrap(); let session=session.as_ref().ok_or_else(||error("model_closed","GPU model is closed"))?;
            let cancel=||cancelled.as_ref().map(|v|Python::with_gil(|py|v.clone_ref(py)));
            let (width,height)=self.output_size; let target=self.state.allocate((width*height*16) as u64)?;
            let pass=|io:&mut Io,mode:u32,tensor:&wgpu::Buffer|->PyResult<()>{
                let mut params=[0u32;16]; params[..12].copy_from_slice(&[image.width as u32,image.height as u32,image.stride as u32,image.offset as u32,
                    width as u32,height as u32,self.channels,self.nchw as u32,mode,self.input_srgb as u32,self.output_srgb as u32,0]);
                params[12]=self.scale.to_bits(); params[13]=self.bias.to_bits();
                let uniform=self.state.uniform("ops-tensor-layout", bytemuck::cast_slice(&params))?;
                let size=if mode==0 {self.input_size} else {self.output_size};
                self.state.dispatch(self.state.encoder(io)?,"tensor_transfer",&[image.storage.buffer.as_ref().unwrap(),tensor,target.buffer.as_ref().unwrap(),&uniform],
                    ((size.0 as u32).div_ceil(8),(size.1 as u32).div_ceil(8)));
                Ok(())
            };
            pass(&mut io,0,&session.input)?; pass(&mut io,2,&session.output[0])?;
            self.state.flush(&mut io,cancel())?;
            let mut message=[0i8;4096]; let status=ops_ort_run(session.handle,message.as_mut_ptr(),message.len());
            self.state.wait(self.state.queue.submit([]),cancel())?;
            if status!=0 { return Err(error("model_execution",CStr::from_ptr(message.as_ptr()).to_string_lossy())); }
            pass(&mut io,1,&session.output[0])?;
            let ticket=self.state.ticket(&mut io);
            self.state.recorded(&mut io,1,cancel())?;
            Ok(GpuImage{storage:target,width,height,generation:self.state.generation,ticket,stride:width,offset:0})
        })
    }

    #[pyo3(signature = (image, cancelled=None))]
    fn run_tensors(&self,py:Python<'_>,image:&GpuImage,cancelled:Option<Py<PyAny>>)->PyResult<Vec<GpuTensor>> {
        self.state.image_check(image)?;
        let shapes=self.tensor_shapes.as_ref().ok_or_else(||error("model_layout","Image model requires run"))?;
        if (image.width,image.height)!=self.input_size { return Err(error("model_layout","GPU input size mismatch")); }
        py.allow_threads(|| unsafe {
            let mut io=self.state.io.lock().unwrap();
            let session=self.session.lock().unwrap(); let session=session.as_ref().ok_or_else(||error("model_closed","GPU model is closed"))?;
            let cancel=||cancelled.as_ref().map(|v|Python::with_gil(|py|v.clone_ref(py)));
            let mut params=[0u32;16];
            params[..12].copy_from_slice(&[image.width as u32,image.height as u32,image.stride as u32,image.offset as u32,
                image.width as u32,image.height as u32,self.channels,self.nchw as u32,0,self.input_srgb as u32,0,0]);
            params[12]=self.scale.to_bits();params[13]=self.bias.to_bits();
            let uniform=self.state.uniform("ops-tensor-input", bytemuck::cast_slice(&params))?;
            let dummy=self.state.allocate(16)?;
            let encoder=self.state.encoder(&mut io)?;
            self.state.dispatch(encoder,"tensor_transfer",&[image.storage.buffer.as_ref().unwrap(),&session.input,dummy.buffer.as_ref().unwrap(),&uniform],
                ((image.width as u32).div_ceil(8),(image.height as u32).div_ceil(8)));
            for (shape,output) in shapes.iter().zip(&session.output) {
                let count=shape.iter().product::<usize>() as u32;
                let groups=count.div_ceil(64).min(self.state.limits.max_compute_workgroups_per_dimension);
                let values=[count,groups*64,0,0];
                let uniform=self.state.uniform("ops-tensor-clear", bytemuck::cast_slice(&values))?;
                self.state.dispatch(encoder,"tensor_clear",&[output,&uniform],(groups,count.div_ceil(groups*64)));
            }
            self.state.flush(&mut io,cancel())?;
            let mut message=[0i8;4096]; let status=ops_ort_run(session.handle,message.as_mut_ptr(),message.len());
            self.state.wait(self.state.queue.submit([]),cancel())?;
            if status!=0 { return Err(error("model_execution",CStr::from_ptr(message.as_ptr()).to_string_lossy())); }
            let mut targets=Vec::new();let encoder=self.state.encoder(&mut io)?;
            for (shape,output) in shapes.iter().zip(&session.output) {
                let bytes=(shape.iter().product::<usize>()*4) as u64;
                let target=self.state.allocate(bytes)?;
                encoder.copy_buffer_to_buffer(output,0,target.buffer.as_ref().unwrap(),0,bytes);
                targets.push(target);
            }
            let event=self.state.flush(&mut io,cancel())?;
            Ok(targets.into_iter().zip(shapes).map(|(storage,shape)|GpuTensor{storage,shape:shape.clone(),event}).collect())
        })
    }

    fn close(&self,py:Python<'_>) {
        py.allow_threads(|| { let _io=self.state.io.lock().unwrap(); self.session.lock().unwrap().take(); });
    }
}
