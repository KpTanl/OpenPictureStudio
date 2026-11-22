struct Params { output:vec4<u32>, source:vec4<u32>, auxiliary:vec4<u32>, field:vec4<u32>, extra:vec4<u32>, args:array<vec4<f32>,12> };
@group(0) @binding(0) var<storage,read> source:array<vec4<f32>>;
@group(0) @binding(1) var<storage,read> auxiliary:array<vec4<f32>>;
@group(0) @binding(2) var<storage,read> fields:array<vec4<f32>>;
@group(0) @binding(3) var<storage,read> extra:array<vec4<f32>>;
@group(0) @binding(4) var<storage,read_write> result:array<vec4<f32>>;
@group(0) @binding(5) var<storage,read> data:array<f32>;
@group(0) @binding(6) var<uniform> p:Params;
fn at(q:vec2<u32>,s:vec4<u32>)->u32 {return s.w+min(q.y,s.y-1u)*s.z+min(q.x,s.x-1u);}
fn load(q:vec2<i32>)->vec4<f32> {return source[at(vec2<u32>(clamp(q,vec2<i32>(0),vec2<i32>(p.source.xy)-1)),p.source)];}
fn reflect(v:i32,n:i32)->i32 {if n<=1 {return 0;}let period=2*n-2;let k=((v%period)+period)%period;return select(k,period-k,k>=n);}
fn reflected(q:vec2<i32>)->vec4<f32> {return load(vec2<i32>(reflect(q.x,i32(p.source.x)),reflect(q.y,i32(p.source.y))));}
fn even(v:vec3<f32>)->vec3<f32> {let lo=floor(v);let f=v-lo;return lo+select(vec3<f32>(0.0),vec3<f32>(1.0),(f>vec3<f32>(.5))|((f==vec3<f32>(.5))&(vec3<i32>(lo)%2!=vec3<i32>(0))));}
fn encoded(v:vec3<f32>)->vec3<f32> {return select(1.055*pow(max(v,vec3<f32>(0.0)),vec3<f32>(1.0/2.4))-.055,v*12.92,v<=vec3<f32>(.0031308));}
fn decoded(v:vec3<f32>)->vec3<f32> {return select(pow(max((v+.055)/1.055,vec3<f32>(0.0)),vec3<f32>(2.4)),v/12.92,v<=vec3<f32>(.04045));}
fn quantized(v:f32)->f32 {
    if !(v>0.0) {return 0.0;}
    var low=0u;var high=255u;
    while low<high {let mid=(low+high+1u)/2u;if v>=data[mid-1u] {low=mid;} else {high=mid-1u;}}
    return f32(low);
}
fn ycc(v:vec3<f32>,floating:bool)->vec3<f32> {
    if floating {let y=dot(v,vec3<f32>(.299,.587,.114));return vec3<f32>(y,(v.r-y)*.713+.5,(v.b-y)*.564+.5);}
    let c=vec3<i32>(v);let y=(c.r*4899+c.g*9617+c.b*1868+8192)>>14;
    return clamp(vec3<f32>(f32(y),f32(((c.r-y)*11682+2105344)>>14),f32(((c.b-y)*9241+2105344)>>14)),vec3<f32>(0.0),vec3<f32>(255.0));
}
fn from_ycc(v:vec3<f32>,floating:bool)->vec3<f32> {
    if floating {let cr=v.y-.5;let cb=v.z-.5;return even(clamp(vec3<f32>(v.x+1.403*cr,v.x-.714*cr-.344*cb,v.x+1.773*cb)*255.0,vec3<f32>(0.0),vec3<f32>(255.0)));}
    let c=vec3<i32>(v);let cr=c.y-128;let cb=c.z-128;
    return clamp(vec3<f32>(f32(c.x+((22987*cr+8192)>>14)),f32(c.x+((-11698*cr-5636*cb+8192)>>14)),f32(c.x+((29049*cb+8192)>>14))),vec3<f32>(0.0),vec3<f32>(255.0));
}
fn ramp(index:u32,length:u32,edge:u32,first:bool,last:bool)->f32 {
    var value=1.0;let count=min(edge,length);
    if count>0u {if !first&&index<count {value=min(value,select(f32(index)/f32(max(1u,count-1u)),0.0,count==1u));}
        if !last&&index>=length-count {value=min(value,select(f32(length-1u-index)/f32(max(1u,count-1u)),0.0,count==1u));}}
    return value;
}
@compute @workgroup_size(8,8)
fn main(@builtin(global_invocation_id) id:vec3<u32>) {
    if any(id.xy>=p.output.xy) {return;}let q=id.xy;let k=q.y*p.output.x+q.x;let mode=p.output.z;
    let s=load(vec2<i32>(q));let a=auxiliary[at(q,p.auxiliary)];let f=fields[at(q,p.field)];let e=extra[at(q,p.extra)];var value=s;
    switch mode {
        case 0u:{value=vec4<f32>(quantized(s.r),quantized(s.g),quantized(s.b),s.a);}
        case 1u:{
            let axis=u32(p.args[0].x);let index=q[axis];let offset=index*3u;let first=u32(data[offset]);let count=u32(data[offset+1u]);let weights=u32(data[offset+2u]);var total=vec3<i32>(2097152);
            for(var i=0u;i<count;i++) {var pos=q;pos[axis]=first+i;total+=vec3<i32>(load(vec2<i32>(pos)).rgb)*i32(data[weights+i]);}
            value=vec4<f32>(clamp(vec3<f32>(total>>vec3<u32>(22u)),vec3<f32>(0.0),vec3<f32>(255.0)),s.a);
        }
        case 2u:{
            let axis=u32(p.args[0].x);let radius=i32(p.args[0].y);var sum=vec3<u32>(0u);
            for(var i=-radius;i<=radius;i++) {var pos=vec2<i32>(q);pos[axis]+=i;sum+=vec3<u32>(load(pos).rgb);}
            var lo=vec2<i32>(q);var hi=lo;lo[axis]-=radius+1;hi[axis]+=radius+1;
            let total=sum*u32(p.args[0].z)+(vec3<u32>(load(lo).rgb)+vec3<u32>(load(hi).rgb))*u32(p.args[0].w)+vec3<u32>(8388608u);
            value=vec4<f32>(vec3<f32>(total>>vec3<u32>(24u)),s.a);
        }
        case 3u:{
            let first=k*1024u;let last=min(first+1024u,p.source.x*p.source.y);var low=3.4e38;var high=-3.4e38;var invalid=0.0;
            for(var i=first;i<last;i++) {let v=load(vec2<i32>(i32(i%p.source.x),i32(i/p.source.x)));let count=select(3u,4u,p.args[0].x!=0.0);
                for(var c=0u;c<count;c++) {if v[c]!=v[c]||abs(v[c])>3.4e38 {invalid=1.0;}else {low=min(low,v[c]);high=max(high,v[c]);}}}
            value=vec4<f32>(low,high,invalid,0.0);
        }
        case 4u:{var low=3.4e38;var high=-3.4e38;var invalid=0.0;for(var i=0u;i<p.source.x*p.source.y;i++) {let v=load(vec2<i32>(i32(i%p.source.x),i32(i/p.source.x)));low=min(low,v.x);high=max(high,v.y);invalid=max(invalid,v.z);}value=vec4<f32>(low,high,invalid,0.0);}
        case 5u:{let maximum=max(auxiliary[p.auxiliary.w].y,1.0);value=vec4<f32>((s.rgb/maximum-vec3<f32>(.485,.456,.406))/vec3<f32>(.229,.224,.225),1.0);}
        case 6u:{let v=source[p.source.w+k/4u][k%4u];value=vec4<f32>(v);}
        case 7u:{let bounds=auxiliary[p.auxiliary.w];var v=clamp(s.x,0.0,1.0);if bounds.y>bounds.x {v=(s.x-bounds.x)/(bounds.y-bounds.x);}value=vec4<f32>(even(vec3<f32>(v*255.0)),1.0);}
        case 8u:{let difference=clamp(encoded(s.rgb),vec3<f32>(0.0),vec3<f32>(1.0))*255.0-p.args[0].rgb;value=vec4<f32>(select(255.0,0.0,length(difference)<=p.args[0].w));}
        case 9u:{let alpha=s.a*a.x/255.0;value=vec4<f32>(s.rgb,alpha);if p.args[0].x>=0.0 {value=vec4<f32>(s.rgb*alpha+p.args[0].x*(1.0-alpha),1.0);}}
        case 10u:{if s.a<1.0 {value=vec4<f32>(decoded(encoded(s.rgb)*s.a+p.args[0].rgb/255.0*(1.0-s.a)),1.0);}else {value.a=1.0;}}
        case 11u:{
            value=vec4<f32>(1.0);let size=p.source.xy;
            for(var i=0u;i<u32(p.args[0].x);i++) {let origin=vec2<u32>(u32(data[i*2u]),u32(data[i*2u+1u]));
                if all(q>=origin)&&all(q<=origin+size) {let pos=q-origin;if pos.x==0u||pos.y==0u||pos.x==size.x||pos.y==size.y {value=vec4<f32>(p.args[0].yzw,1.0);}else {value=load(vec2<i32>(pos));}}}
        }
        case 12u:{let floating=p.args[0].x!=0.0;value=vec4<f32>(ycc(s.rgb/select(1.0,255.0,floating),floating),s.a);}
        case 13u:{value=vec4<f32>(from_ycc(s.rgb,p.args[0].x!=0.0),s.a);}
        case 14u:{value=vec4<f32>(s.x,a.x,f.x,e.a);}
        case 15u:{
            let axis=u32(p.args[0].x);let radius=i32(p.args[0].y);var total=vec3<f32>(0.0);
            for(var i=-radius;i<=radius;i++) {var pos=vec2<i32>(q);pos[axis]+=i;total+=reflected(pos).rgb*data[u32(i+radius)];}
            value=vec4<f32>(total,s.a);
        }
        case 16u:{
            let radius=i32(p.args[0].x);let count=u32((radius*2+1)*(radius*2+1));var samples:array<f32,121>;var n=0u;
            for(var y=-radius;y<=radius;y++) {for(var x=-radius;x<=radius;x++) {let v=load(vec2<i32>(q)+vec2<i32>(x,y)).x;var j=n;while j>0u {if samples[j-1u]<=v {break;}samples[j]=samples[j-1u];j--;}samples[j]=v;n++;}}
            value=vec4<f32>(samples[count/2u]);
        }
        case 17u:{
            let radius=i32(p.args[0].x);let color=p.args[0].y;let space=p.args[0].z;var total=0.0;var mass=0.0;
            for(var y=-radius;y<=radius;y++) {for(var x=-radius;x<=radius;x++) {let distance=x*x+y*y;if distance>radius*radius {continue;}
                let v=reflected(vec2<i32>(q)+vec2<i32>(x,y)).x;let difference=v-s.x;let weight=exp(-.5*f32(distance)/(space*space)-.5*difference*difference/(color*color));total+=weight*v;mass+=weight;}}
            value=vec4<f32>(total/mass);
        }
        case 18u:{let v=select(even(s.rgb),s.rgb,p.args[0].y!=0.0);value=vec4<f32>(a.rgb+(v-a.rgb)*p.args[0].x,s.a);}
        case 19u:{
            if q.x>0u&&q.y>0u&&q.x+1u<p.source.x&&q.y+1u<p.source.y {var sum=vec3<u32>(vec3<u32>(s.rgb)*4u);
                for(var y=-1;y<=1;y++) {for(var x=-1;x<=1;x++) {sum+=vec3<u32>(load(vec2<i32>(q)+vec2<i32>(x,y)).rgb);}}
                let blurred=vec3<f32>((sum+vec3<u32>(6u))/13u);value=vec4<f32>(floor(clamp(blurred+(s.rgb-blurred)*(1.0+p.args[0].x),vec3<f32>(0.0),vec3<f32>(255.0))),s.a);}
        }
        case 20u:{let pos=vec2<i32>(reflect(i32(q.x),i32(p.args[0].z)),reflect(i32(q.y),i32(p.args[0].w)))+vec2<i32>(p.args[0].xy);value=load(pos)/255.0;value.a=1.0;}
        case 21u:{let range=auxiliary[p.auxiliary.w];let v=select(s.rgb,(s.rgb+1.0)*.5,range.y>1.5||range.x<-.2);value=vec4<f32>(clamp(v,vec3<f32>(0.0),vec3<f32>(1.0)),1.0);}
        case 22u:{
            let origin=vec2<u32>(p.args[0].xy);let size=vec2<u32>(p.args[0].zw);
            if all(q>=origin)&&all(q<origin+size) {let pos=q-origin;let overlap=u32(p.args[1].x);
                let weight=max(.001,ramp(pos.x,size.x,overlap,origin.x==0u,origin.x+size.x==p.output.x)*ramp(pos.y,size.y,overlap,origin.y==0u,origin.y+size.y==p.output.y));
                value=s+vec4<f32>(auxiliary[at(pos,p.auxiliary)].rgb*weight,weight);}
        }
        case 23u:{value=vec4<f32>(even(clamp(s.rgb/max(s.a,1e-6),vec3<f32>(0.0),vec3<f32>(1.0))*255.0),1.0);}
        case 24u:{value=vec4<f32>(floor(clamp(s.rgb*(1.0-p.args[0].xyy)+a.rgb*p.args[0].xyy,vec3<f32>(0.0),vec3<f32>(255.0))),1.0);}
        case 25u:{value=vec4<f32>(decoded(s.rgb/255.0),a.a);}
        case 26u:{value=vec4<f32>(s.rgb/255.0,1.0);}
        case 27u:{value=vec4<f32>(s.rgb,clamp(a.x/255.0,0.0,1.0)*s.a);}
        default:{value=s+a*p.args[0].x+f*p.args[0].y+e*p.args[0].z;}
    }
    result[k]=value;
}
