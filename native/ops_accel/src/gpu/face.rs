use super::*;

impl GpuDevice {
    pub(super) fn face_image(&self, py:Python<'_>, image:&GpuImage, auxiliary:&GpuImage,
        a:Option<&GpuTensor>, mode:u32, args:Vec<f32>, data:Vec<f32>,
        width:usize,height:usize,cancelled:Option<Py<PyAny>>)->PyResult<GpuImage> {
        self.state.image_check(image)?; self.state.image_check(auxiliary)?;
        if let Some(tensor) = a {
            if !Arc::ptr_eq(&tensor.storage.state,&self.state) { return Err(error("device_changed","Face tensor belongs to another device")); }
        }
        if mode>18||args.len()>32||data.len()>4096||args.iter().chain(&data).any(|v|!v.is_finite())
            ||width==0||height==0||width>32768||height>65536 {
            return Err(error("face_specification","Invalid face operation"));
        }
        let tensor_size=a.map_or(0,|tensor|tensor.storage.size/4);
        if (mode==2&&(tensor_size<15232||width!=5||height!=896))
            ||(mode==5&&(tensor_size<1435||width!=478||height!=1))
            ||(mode==6&&(image.width!=478||image.height!=1||data.len()!=68||width!=486||height!=1))
            ||(mode==3&&(width!=2||args.len()<6||image.width!=5||image.height>65536
                ||(args[0]==0.0&&(image.height!=896||height>5))
                ||(args[0]!=0.0&&(args[3]<1.0||args[3]>60.0||height<args[3] as usize+5000))))
            ||(mode==12&&(tensor_size<(height*16) as u64||data.len()!=12||width!=5)) {
            return Err(error("face_specification","Face tensor or output shape mismatch"));
        }
        py.allow_threads(|| {
            let _io=self.state.io.lock().unwrap();
            let cancel=||cancelled.as_ref().map(|v|Python::with_gil(|py|v.clone_ref(py)));
            Cancellation::new(cancel()).check()?;
            let target=self.state.allocate((width*height*16) as u64)?;
            let mut params=[0u32;48];
            params[..12].copy_from_slice(&[width as u32,height as u32,mode,0,image.width as u32,image.height as u32,
                image.stride as u32,image.offset as u32,auxiliary.width as u32,auxiliary.height as u32,auxiliary.stride as u32,auxiliary.offset as u32]);
            for (index,value) in args.iter().enumerate() {params[index+16]=value.to_bits();}
            let uniform=self.state.uniform("ops-face-parameters", bytemuck::cast_slice(&params))?;
            let metadata=self.state.allocate((data.len().max(1)*4) as u64)?;
            if !data.is_empty() {
                let mut pool=self.state.pool.lock().unwrap();
                if pool.total()+metadata.size>pool.limit {pool.idle.clear();}
                if pool.total()+metadata.size>pool.limit {return Err(error("memory_pressure","Face metadata exceeds GPU budget"));}
                pool.transient=metadata.size;
                self.state.queue.write_buffer(metadata.buffer.as_ref().unwrap(),0,bytemuck::cast_slice(&data));
            }
            let mut encoder=self.state.device.create_command_encoder(&Default::default());
            self.state.dispatch(&mut encoder,"face",&[image.storage.buffer.as_ref().unwrap(),auxiliary.storage.buffer.as_ref().unwrap(),
                a.map_or(image.storage.buffer.as_ref().unwrap(),|v|v.storage.buffer.as_ref().unwrap()),
                target.buffer.as_ref().unwrap(),metadata.buffer.as_ref().unwrap(),&uniform],
                ((width as u32).div_ceil(8),(height as u32).div_ceil(8)));
            let event=self.state.wait(self.state.queue.submit([encoder.finish()]),cancel());
            self.state.pool.lock().unwrap().transient=0;
            Ok(GpuImage{storage:target,width,height,generation:self.state.generation,event:event?,stride:width,offset:0})
        })
    }

    pub(super) fn join_tensors(&self,py:Python<'_>,tensors:Vec<PyRef<'_,GpuTensor>>,cancelled:Option<Py<PyAny>>)->PyResult<GpuTensor> {
        if tensors.is_empty()||tensors.iter().any(|tensor|!Arc::ptr_eq(&tensor.storage.state,&self.state)) {
            return Err(error("model_layout","Invalid GPU tensor bundle"));
        }
        let allocations:Vec<_>=tensors.iter().map(|tensor|tensor.storage.clone()).collect();
        py.allow_threads(|| {
            let _io=self.state.io.lock().unwrap();
            let bytes=allocations.iter().map(|v|v.size).sum();let storage=self.state.allocate(bytes)?;
            let mut encoder=self.state.device.create_command_encoder(&Default::default());let mut offset=0;
            for allocation in &allocations {
                encoder.copy_buffer_to_buffer(allocation.buffer.as_ref().unwrap(),0,storage.buffer.as_ref().unwrap(),offset,allocation.size);
                offset+=allocation.size;
            }
            let event=self.state.wait(self.state.queue.submit([encoder.finish()]),cancelled)?;
            Ok(GpuTensor{storage,shape:vec![bytes as usize/4],event})
        })
    }
}
