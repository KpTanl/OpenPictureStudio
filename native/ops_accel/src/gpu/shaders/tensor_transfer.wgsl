struct Params { source:vec4<u32>, destination:vec4<u32>, options:vec4<u32>, values:vec4<f32> };
@group(0) @binding(0) var<storage,read> source:array<vec4<f32>>;
@group(0) @binding(1) var<storage,read_write> tensor:array<f32>;
@group(0) @binding(2) var<storage,read_write> result:array<vec4<f32>>;
@group(0) @binding(3) var<uniform> p:Params;
fn encode(rgb:vec3<f32>)->vec3<f32> { let x=clamp(rgb,vec3<f32>(0.0),vec3<f32>(1.0)); return select(1.055*pow(x,vec3<f32>(1.0/2.4))-0.055,12.92*x,x<=vec3<f32>(0.0031308)); }
fn decode(rgb:vec3<f32>)->vec3<f32> { return select(pow(max((rgb+0.055)/1.055,vec3<f32>(0.0)),vec3<f32>(2.4)),rgb/12.92,rgb<=vec3<f32>(0.04045)); }
fn alpha(x:i32,y:i32)->f32 { return source[p.source.w+u32(clamp(y,0,i32(p.source.y)-1))*p.source.z+u32(clamp(x,0,i32(p.source.x)-1))].a; }
@compute @workgroup_size(8,8)
fn main(@builtin(global_invocation_id) id:vec3<u32>) {
    let mode=p.options.x; let size=select(p.destination.xy,p.source.xy,mode==0u);
    if any(id.xy>=size) { return; }
    let index=id.y*size.x+id.x; let count=size.x*size.y; let channels=p.destination.z;
    if mode==0u {
        var pixel=source[p.source.w+id.y*p.source.z+id.x];
        if p.options.y!=0u { pixel=vec4<f32>(encode(pixel.rgb),pixel.a); }
        pixel=pixel*p.values.x+p.values.y;
        for(var c=0u;c<channels;c++) { let at=select(index*channels+c,c*count+index,p.destination.w!=0u); tensor[at]=pixel[c]; }
    } else if mode==2u {
        for(var c=0u;c<channels;c++) { tensor[index*channels+c]=0.0; }
    } else {
        var pixel=vec4<f32>(0.0,0.0,0.0,1.0);
        for(var c=0u;c<channels;c++) { let at=select(index*channels+c,c*count+index,p.destination.w!=0u); pixel[c]=tensor[at]; }
        if channels==3u {
            let uv=clamp((vec2<f32>(id.xy)+0.5)*vec2<f32>(p.source.xy)/vec2<f32>(size)-0.5,vec2<f32>(0.0),vec2<f32>(p.source.xy)-1.0);
            let q=vec2<i32>(floor(uv)); let t=fract(uv);
            pixel.a=mix(mix(alpha(q.x,q.y),alpha(q.x+1,q.y),t.x),mix(alpha(q.x,q.y+1),alpha(q.x+1,q.y+1),t.x),t.y);
        }
        if p.options.z!=0u { pixel=vec4<f32>(decode(pixel.rgb),pixel.a); }
        result[index]=vec4<f32>(pixel.rgb,clamp(pixel.a,0.0,1.0));
    }
}
