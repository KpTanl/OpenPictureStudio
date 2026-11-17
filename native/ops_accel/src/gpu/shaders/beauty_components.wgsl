struct Params { source:vec4<u32>, roots:vec4<u32>, args:vec4<f32>, mode:vec4<u32> };
@group(0) @binding(0) var<storage,read> source:array<vec4<f32>>;
@group(0) @binding(1) var<storage,read> roots:array<vec4<f32>>;
@group(0) @binding(2) var<storage,read_write> result:array<vec4<f32>>;
@group(0) @binding(3) var<uniform> p:Params;
@group(0) @binding(4) var<storage,read_write> stats:array<atomic<u32>>;
fn even_round(v:f32)->f32 {let lo=floor(v);let t=v-lo;return lo+select(0.0,1.0,t>0.5||(t==0.5&&i32(lo)%2!=0));}
fn root(index:u32)->f32 {return roots[p.roots.w+(index/p.roots.x)*p.roots.z+index%p.roots.x].x;}
@compute @workgroup_size(256)
fn main(@builtin(global_invocation_id) id:vec3<u32>) {
    let count=p.roots.x*p.roots.y;let i=id.x;
    switch p.mode.x {
        case 0u:{if i<count {let s=i*8u;
            atomicStore(&stats[s],p.source.x);atomicStore(&stats[s+1u],p.source.y);atomicStore(&stats[s+2u],0u);atomicStore(&stats[s+3u],0u);
            atomicStore(&stats[s+4u],0u);atomicStore(&stats[s+5u],0u);atomicStore(&stats[s+6u],0u);atomicStore(&stats[s+7u],0xffffffffu);}}
        case 1u:{if i<p.source.x*p.source.y {
            let pos=vec2<u32>(i%p.source.x,i/p.source.x);let label=source[p.source.w+pos.y*p.source.z+pos.x].x;
            if label>0.0 {
                // 斑点的根按像素序号升序排列
                var lo=0u;var hi=count;
                while lo<hi {let mid=(lo+hi)/2u;if root(mid)<label {lo=mid+1u;}else {hi=mid;}}
                if lo<count&&root(lo)==label {let s=lo*8u;
                    atomicMin(&stats[s],pos.x);atomicMin(&stats[s+1u],pos.y);atomicMax(&stats[s+2u],pos.x);atomicMax(&stats[s+3u],pos.y);
                    atomicAdd(&stats[s+4u],1u);atomicAdd(&stats[s+5u],pos.x);atomicAdd(&stats[s+6u],pos.y);
                    atomicMin(&stats[s+7u],(pos.y/2u)*((p.source.x+1u)/2u)+pos.x/2u);}
            }}}
        default:{if i<count {let s=i*8u;
            let lo=vec2<u32>(atomicLoad(&stats[s]),atomicLoad(&stats[s+1u]));let hi=vec2<u32>(atomicLoad(&stats[s+2u]),atomicLoad(&stats[s+3u]));
            let n=atomicLoad(&stats[s+4u]);let total=vec2<u32>(atomicLoad(&stats[s+5u]),atomicLoad(&stats[s+6u]));let d=p.args.x;
            let diameter=2.0*sqrt(f32(n)/3.141592653589793);let pad=max(1.0,even_round(.03*d));
            let first=i*4u;
            result[first]=vec4<f32>(vec2<f32>(lo),vec2<f32>(hi-lo+1u));
            result[first+1u]=vec4<f32>(f32(n),vec2<f32>(total)/max(1.0,f32(n)),root(i));
            result[first+2u]=vec4<f32>(max(vec2<f32>(0.0),vec2<f32>(lo)-pad),min(vec2<f32>(p.source.xy),vec2<f32>(hi)+1.0+pad));
            result[first+3u]=vec4<f32>(f32(atomicLoad(&stats[s+7u])),diameter,select(0.0,1.0,diameter>=.01*d&&diameter<=.04*d),0.0);}}
    }
}
