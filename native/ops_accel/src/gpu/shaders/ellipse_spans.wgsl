struct Params { a:u32, b:u32, pad0:u32, pad1:u32 };
@group(0) @binding(0) var<storage,read_write> spans:array<vec4<f32>>;
@group(0) @binding(1) var<uniform> p:Params;
fn mul(a:u32,b:u32)->vec2<u32> {
    let a0=a&65535u; let a1=a>>16u; let b0=b&65535u; let b1=b>>16u;
    let q=a0*b0; let t=a1*b0+(q>>16u); let s=a0*b1+(t&65535u);
    return vec2<u32>((s<<16u)|(q&65535u),a1*b1+(t>>16u)+(s>>16u));
}
fn add(a:vec2<u32>,b:vec2<u32>)->vec2<u32> { let lo=a.x+b.x; return vec2<u32>(lo,a.y+b.y+select(0u,1u,lo<a.x)); }
fn sub(a:vec2<u32>,b:vec2<u32>)->vec2<u32> { return vec2<u32>(a.x-b.x,a.y-b.y-select(0u,1u,a.x<b.x)); }
fn greater(a:vec2<u32>,b:vec2<u32>)->bool { return a.y>b.y||(a.y==b.y&&a.x>b.x); }
fn delta(x:u32,y:u32)->vec2<u32> {
    let a2=p.a*p.a; let b2=p.b*p.b; let v=add(mul(a2,y*y),mul(b2,x*x)); let base=mul(a2,b2);
    if greater(v,base) { return sub(v,base); } return sub(base,v);
}
@compute @workgroup_size(1)
fn main() {
    var x=p.a; var y=p.b%2u; var previous=0xffffffffu;
    loop {
        if previous!=y {
            let span=vec4<f32>(f32((p.a-x)/2u),f32((p.a+x)/2u),0.0,0.0);
            spans[(p.b+y)/2u]=span; spans[(p.b-y)/2u]=span; previous=y;
        }
        if x==p.a%2u&&y==p.b { break; }
        var nx=x; var ny=y+2u; var best=delta(nx,ny);
        if x>1u {
            let diagonal=delta(x-2u,y+2u); if greater(best,diagonal) { nx=x-2u; ny=y+2u; best=diagonal; }
            let horizontal=delta(x-2u,y); if greater(best,horizontal) { nx=x-2u; ny=y; }
        }
        x=nx; y=ny;
    }
}
