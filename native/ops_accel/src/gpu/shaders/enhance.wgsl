struct Params { output:vec4<u32>, source:vec4<u32>, auxiliary:vec4<u32>, field:vec4<u32>, extra:vec4<u32>, args:array<vec4<f32>,12> };
@group(0) @binding(0) var<storage,read> source:array<vec4<f32>>;
@group(0) @binding(1) var<storage,read> auxiliary:array<vec4<f32>>;
@group(0) @binding(2) var<storage,read> fields:array<vec4<f32>>;
@group(0) @binding(3) var<storage,read> extra:array<vec4<f32>>;
@group(0) @binding(4) var<storage,read_write> result:array<vec4<f32>>;
@group(0) @binding(5) var<storage,read> data:array<f32>;
@group(0) @binding(6) var<uniform> p:Params;
fn at(q:vec2<u32>,s:vec4<u32>)->u32 {return s.w+q.y*s.z+q.x;}
fn load(q:vec2<u32>)->vec4<f32> {return source[at(q,p.source)];}
fn lut(v:f32)->f32 {let t=clamp(v,0.0,1.0)*4095.0;let k=u32(t);return mix(data[k],data[min(k+1u,4095u)],fract(t));}
fn nearest_chunk() {
    let size=p.output.xy;let count=size.x*size.y;let phase=u32(p.args[0].y);
    let offsets=array<vec2<i32>,8>(vec2<i32>(-1,-2),vec2<i32>(1,-2),vec2<i32>(-2,-1),vec2<i32>(-1,-1),vec2<i32>(0,-1),vec2<i32>(1,-1),vec2<i32>(2,-1),vec2<i32>(-1,0));
    let costs=array<u32,8>(143976u,143976u,143976u,91750u,65536u,91750u,143976u,65536u);
    let last=min(count,p.output.w+u32(p.args[0].x));
    for(var step=p.output.w;step<last;step++) {
        let k=select(step,count-1u-step,phase==1u);var best=result[k];var distance=bitcast<u32>(best.x);if distance==0u {continue;}
        let q=vec2<i32>(i32(k%size.x),i32(k/size.x));
        for(var i=0u;i<8u;i++) {let pos=q+offsets[i]*select(1,-1,phase==1u);if any(pos<vec2<i32>(0))||any(pos>=vec2<i32>(size)) {continue;}
            let candidate=result[u32(pos.y)*size.x+u32(pos.x)];let d=bitcast<u32>(candidate.x)+costs[i];if d<distance {distance=d;best=vec4<f32>(bitcast<f32>(d),candidate.yzw);}}
        result[k]=best;
    }
}
@compute @workgroup_size(8,8)
fn main(@builtin(global_invocation_id) id:vec3<u32>) {
    if any(id.xy>=p.output.xy) {return;}let q=id.xy;let mode=p.output.z;let k=q.y*p.output.x+q.x;
    if mode==17u {if k==0u {nearest_chunk();}return;}
    var value=load(min(q,p.source.xy-1u));
    switch mode {
        case 0u:{let v=clamp(value.rgb,vec3<f32>(0.0),vec3<f32>(1.0));value=vec4<f32>(select(1.055*pow(v,vec3<f32>(1.0/2.4))-.055,12.92*v,v<=vec3<f32>(.0031308)),value.a);}
        case 8u:{let v=value.rgb;value=vec4<f32>(select(pow(max((v+.055)/1.055,vec3<f32>(0.0)),vec3<f32>(2.4)),v/12.92,v<=vec3<f32>(.04045)),value.a);}
        case 2u:{if value.a<=1e-6 {let nearest_pixel=auxiliary[at(q,p.auxiliary)];value=vec4<f32>(0.0,0.0,0.0,value.a);if nearest_pixel.a>0.0 {value=vec4<f32>(load(vec2<u32>(nearest_pixel.yz)).rgb,value.a);}}}
        case 3u:{let limit=min(p.source.xy,vec2<u32>(p.args[0].zw)+u32(p.args[1].x))-1u;
            let pos=vec2<u32>(clamp(vec2<i32>(q)+vec2<i32>(p.args[0].xy)-i32(p.args[1].x),vec2<i32>(0),vec2<i32>(limit)));value=load(pos);}
        case 4u:{value=load(q+vec2<u32>(p.args[0].xy));value=vec4<f32>(clamp(value.rgb,vec3<f32>(0.0),vec3<f32>(1.0)),value.a);}
        case 5u:{var valid=true;let first=k*1024u;let end=min(first+1024u,p.source.x*p.source.y);for(var i=first;i<end;i++) {let v=load(vec2<u32>(i%p.source.x,i/p.source.x)).rgb;valid=valid&&all(v==v)&&all(abs(v)<vec3<f32>(3.4e38));}value=vec4<f32>(select(1.0,0.0,valid));}
        case 6u:{let restored=auxiliary[at(q,p.auxiliary)].rgb;let dy=dot(restored-value.rgb,vec3<f32>(.299,.587,.114));
            value=vec4<f32>(clamp(value.rgb+dy*p.args[0].x+(restored-value.rgb-dy)*p.args[0].y,vec3<f32>(0.0),vec3<f32>(1.0)),value.a);}
        case 7u:{value=vec4<f32>(value.rgb+(auxiliary[at(q,p.auxiliary)].rgb-value.rgb)*p.args[0].x,value.a);}
        case 9u:{let pos=vec2<i32>(q)-vec2<i32>(p.args[0].xy);value=vec4<f32>(0.0);if all(pos>=vec2<i32>(0))&&all(pos<vec2<i32>(p.source.xy)) {value=load(vec2<u32>(pos));}}
        case 10u:{value=vec4<f32>(value.rgb,clamp(auxiliary[at(q,p.auxiliary)].x,0.0,1.0));}
        case 11u:{value.a=clamp(value.a,0.0,1.0);}
        case 12u:{value=vec4<f32>(value.a);}
        case 13u:{var count=0u;let first=k*1024u;let end=min(first+1024u,p.source.x*p.source.y);for(var i=first;i<end;i++) {if load(vec2<u32>(i%p.source.x,i/p.source.x)).a<=1e-6 {count++;}}value=vec4<f32>(f32(count),0.0,0.0,0.0);}
        case 14u:{value=vec4<f32>(0.0,0.0,0.0,value.a);}
        case 15u:{value=vec4<f32>(lut(value.r),lut(value.g),lut(value.b),value.a);}
        case 16u:{let visible=value.a>1e-6;value=vec4<f32>(bitcast<f32>(select(4294823319u,0u,visible)),vec2<f32>(q),select(0.0,1.0,visible));}
        default:{value+=auxiliary[at(q,p.auxiliary)]*p.args[0].x+fields[at(q,p.field)]*p.args[0].y+extra[at(q,p.extra)]*p.args[0].z;}
    }
    result[k]=value;
}
