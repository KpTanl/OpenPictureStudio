struct Params { w:u32,h:u32,r:u32,axis:u32,ww:u32,fw:u32,pad0:u32,pad1:u32 };
@group(0) @binding(0) var<storage,read> source:array<vec4<f32>>;
@group(0) @binding(1) var<storage,read_write> result:array<vec4<f32>>;
@group(0) @binding(2) var<uniform> p:Params;
fn load(x:i32,y:i32)->u32 { return u32(source[u32(clamp(y,0,i32(p.h)-1))*p.w+u32(clamp(x,0,i32(p.w)-1))].x); }
@compute @workgroup_size(8,8)
fn main(@builtin(global_invocation_id) id:vec3<u32>) {
    if id.x>=p.w||id.y>=p.h { return; }
    let x=i32(id.x); let y=i32(id.y); let r=i32(p.r); var sum=0u;
    for(var n=-r;n<=r;n++) { sum+=load(x+select(0,n,p.axis==0u),y+select(n,0,p.axis==0u)); }
    let far=load(x+select(0,r+1,p.axis==0u),y+select(r+1,0,p.axis==0u))+load(x-select(0,r+1,p.axis==0u),y-select(r+1,0,p.axis==0u));
    result[id.y*p.w+id.x]=vec4<f32>(f32((sum*p.ww+far*p.fw+8388608u)>>24u));
}
