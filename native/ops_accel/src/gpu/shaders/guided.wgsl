struct Params { w:u32, h:u32, mode:u32, eps:f32 };
@group(0) @binding(0) var<storage,read> a:array<vec4<f32>>;
@group(0) @binding(1) var<storage,read> b:array<vec4<f32>>;
@group(0) @binding(2) var<storage,read> c:array<vec4<f32>>;
@group(0) @binding(3) var<storage,read> center_g:array<vec4<f32>>;
@group(0) @binding(4) var<storage,read> center_p:array<vec4<f32>>;
@group(0) @binding(5) var<storage,read_write> result:array<vec4<f32>>;
@group(0) @binding(6) var<uniform> p:Params;
@compute @workgroup_size(8,8)
fn main(@builtin(global_invocation_id) id:vec3<u32>) {
    if id.x>=p.w || id.y>=p.h { return; }
    let k=id.y*p.w+id.x; let g=a[k]; let s=b[k]; let t=c[k];
    var value:vec4<f32>;
    switch p.mode {
        case 0u: { let centered=g.x-center_g[0].x; value=vec4<f32>(centered,centered*centered,0.0,0.0); }
        case 1u: { value=(g.x-center_g[0].x)*(s-center_p[0]); }
        case 2u: { value=(t-g.x*s)/(g.y-g.x*g.x+p.eps); }
        case 3u: { value=s-t*g.x; }
        case 5u: { value=s-center_p[0]; }
        default: { value=s*(g.x-center_g[0].x)+t+center_p[0]; }
    }
    result[k]=value;
}
