use super::*;

impl State {
    pub(super) fn empty_image(self: &Arc<Self>, io: &mut Io, width: usize, height: usize, cancelled: Option<Py<PyAny>>) -> PyResult<GpuImage> {
        self.check()?;
        if width == 0 || height == 0 || width.max(height) > 32768 { return Err(error("target_limit", "Invalid raster size")); }
        let storage = self.allocate((width * height * 16) as u64)?;
        self.encoder(io)?.clear_buffer(storage.buffer.as_ref().unwrap(), 0, None);
        let ticket = self.ticket(io);
        self.recorded(io, 1, cancelled)?;
        Ok(GpuImage { storage, width, height, generation: self.generation, ticket, stride: width, offset: 0 })
    }

    fn ellipse(self: &Arc<Self>, io: &mut Io, width: usize, height: usize, cancelled: Option<Py<PyAny>>) -> PyResult<GpuImage> {
        let storage = self.allocate((height * 16) as u64)?;
        let uniform = self.uniform("ops-ellipse-params", bytemuck::cast_slice(&[(width-1) as u32,(height-1) as u32,0,0]))?;
        self.dispatch(self.encoder(io)?, "ellipse_spans", &[storage.buffer.as_ref().unwrap(), &uniform], (1,1));
        let ticket = self.ticket(io);
        self.recorded(io, 1, cancelled)?;
        Ok(GpuImage { storage, width: 1, height, generation: self.generation, ticket, stride: 1, offset: 0 })
    }
}

impl GpuDevice {
    pub(super) fn raster_shape(&self, py: Python<'_>, width: usize, height: usize, primitives: Vec<Vec<i32>>,
                               (top, full): (usize, usize), cancelled: Option<Py<PyAny>>) -> PyResult<GpuImage> {
        if primitives.len()>8 || top + height > full || primitives.iter().any(|v| v.len()!=9 || !(0..=1).contains(&v[0])
            || v[1]<0 || v[2]<0 || v[3]<v[1] || v[4]<v[2] || v[3]>=width as i32 || v[4]>=full as i32) {
            return Err(error("target_limit", "Invalid shape primitives"));
        }
        py.allow_threads(|| {
            let mut io = self.state.io.lock().unwrap();
            let cancel=|| cancelled.as_ref().map(|v| Python::with_gil(|py| v.clone_ref(py)));
            let mut mask=self.state.empty_image(&mut io,width,height,cancel())?;
            for primitive in primitives {
                let shape=if primitive[0]==1 { self.state.ellipse(&mut io,(primitive[3]-primitive[1]+1) as usize,
                    (primitive[4]-primitive[2]+1) as usize,cancel())? } else { self.state.ellipse(&mut io,1,1,cancel())? };
                let args:Vec<f32>=primitive[1..].iter().map(|v| *v as f32).chain([primitive[0] as f32, top as f32]).collect();
                mask=self.state.image_op(&mut io,&mask,&shape,12,&args,width,height,cancel())?;
            }
            Ok(mask)
        })
    }

    pub(super) fn raster_lasso(&self, py: Python<'_>, width: usize, height: usize,
        regions: Vec<(bool,Vec<(i32,i32)>)>, invert: bool, feather: f32, (top, full): (usize, usize),
        cancelled: Option<Py<PyAny>>) -> PyResult<GpuImage> {
        if regions.len()>129 || top + height > full
            || regions.iter().any(|(_,v)| v.len()>512 || v.iter().any(|(x,y)| *x<0 || *y<0 || *x>=width as i32 || *y>=full as i32))
            || !feather.is_finite() || !(0.0..=50.0).contains(&feather) {
            return Err(error("target_limit", "Unsupported lasso specification"));
        }
        py.allow_threads(|| {
            let mut io=self.state.io.lock().unwrap();
            let cancel=|| cancelled.as_ref().map(|v| Python::with_gil(|py| v.clone_ref(py)));
            let mut mask=self.state.empty_image(&mut io,width,height,cancel())?;
            for (subtract,points) in regions {
                if points.len()<3 { continue; }
                let bytes=points.len()*16;
                let packed:Vec<f32>=points.iter().zip(points.iter().cycle().skip(1)).take(points.len())
                    .flat_map(|((x,y),(u,v))| [*x as f32,*y as f32,*u as f32,*v as f32]).collect();
                let edges=self.state.data("ops-polygon-edges",&packed)?;
                self.state.uploaded.fetch_add(bytes as u64,Ordering::Relaxed);
                let target=self.state.allocate((width*height*16) as u64)?;
                let uniform=self.state.uniform("ops-polygon-params", bytemuck::cast_slice(&[width as u32,height as u32,points.len() as u32,subtract as u32,top as u32,0,0,0]))?;
                self.state.dispatch(self.state.encoder(&mut io)?,"polygon_mask",&[mask.storage.buffer.as_ref().unwrap(),&edges,
                    target.buffer.as_ref().unwrap(),&uniform],((height as u32).div_ceil(8),1));
                let ticket=self.state.ticket(&mut io);
                self.state.recorded(&mut io,1,cancel())?;
                mask=GpuImage {storage:target,width,height,generation:self.state.generation,ticket,stride:width,offset:0};
            }
            if invert { mask=self.state.image_op(&mut io,&mask,&mask,13,&[],width,height,cancel())?; }
            if feather>0.0 {
                let sigma2=feather*feather/3.0; let length=(12.0*sigma2+1.0).sqrt(); let l=((length-1.0)/2.0).floor();
                let fraction=(2.0*l+1.0)*(l*(l+1.0)-3.0*sigma2)/(6.0*(sigma2-(l+1.0)*(l+1.0)));
                let radius=l+fraction; let r=radius as u32;
                let ww=(16777216.0f32/(radius*2.0+1.0)) as u32; let fw=(16777216-(r*2+1)*ww)/2;
                for axis in 0..2 { for _ in 0..3 {
                    let target=self.state.allocate((width*height*16) as u64)?;
                    let uniform=self.state.uniform("ops-feather-params", bytemuck::cast_slice(&[width as u32,height as u32,r,axis,ww,fw,0,0]))?;
                    self.state.dispatch(self.state.encoder(&mut io)?,"feather",&[mask.storage.buffer.as_ref().unwrap(),target.buffer.as_ref().unwrap(),&uniform],
                        ((width as u32).div_ceil(8),(height as u32).div_ceil(8)));
                    let ticket=self.state.ticket(&mut io);
                    self.state.recorded(&mut io,1,cancel())?;
                    mask=GpuImage {storage:target,width,height,generation:self.state.generation,ticket,stride:width,offset:0};
                }}
            }
            Ok(mask)
        })
    }
}
