struct Params { w:u32, h:u32, radius:u32, axis:u32 };
@group(0) @binding(0) var<storage,read> source:array<vec4<f32>>;
@group(0) @binding(1) var<storage,read_write> result:array<vec4<f32>>;
@group(0) @binding(2) var<uniform> p:Params;
fn index(line:u32, at:u32)->u32 { return select(at*p.w+line,line*p.w+at,p.axis==0u); }
@compute @workgroup_size(16)
fn main(@builtin(global_invocation_id) id:vec3<u32>) {
    let lines=select(p.w,p.h,p.axis==0u); let length=select(p.h,p.w,p.axis==0u);
    if id.x>=lines { return; }
    let r=i32(p.radius); let period=max(64,2*r+1);
    var sum=vec4<f32>(0.0); var correction=vec4<f32>(0.0);
    for(var x=0;x<i32(length);x++) {
        if x%period==0 {
            sum=vec4<f32>(0.0); correction=vec4<f32>(0.0);
            for(var n=max(0,x-r);n<min(i32(length),x+r+1);n++) {
                let v=source[index(id.x,u32(n))]; let total=sum+v;
                correction+=select((v-total)+sum,(sum-total)+v,abs(sum)>=abs(v)); sum=total;
            }
        } else {
            for(var side=0;side<2;side++) {
                let n=select(x-r-1,x+r,side==1); if n<0 || n>=i32(length) { continue; }
                let v=source[index(id.x,u32(n))]*select(-1.0,1.0,side==1); let total=sum+v;
                correction+=select((v-total)+sum,(sum-total)+v,abs(sum)>=abs(v)); sum=total;
            }
        }
        result[index(id.x,u32(x))]=(sum+correction)/f32(min(i32(length),x+r+1)-max(0,x-r));
    }
}
