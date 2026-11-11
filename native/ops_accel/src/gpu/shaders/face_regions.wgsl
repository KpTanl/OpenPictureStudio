struct Params { output:vec4<u32>, source:vec4<u32>, auxiliary:vec4<u32>, field:vec4<u32>, extra:vec4<u32>, args:array<vec4<f32>,8> };
@group(0) @binding(0) var<storage,read> source:array<vec4<f32>>;
@group(0) @binding(1) var<storage,read> auxiliary:array<vec4<f32>>;
@group(0) @binding(2) var<storage,read> fields:array<vec4<f32>>;
@group(0) @binding(3) var<storage,read> extra:array<vec4<f32>>;
@group(0) @binding(4) var<storage,read_write> result:array<vec4<f32>>;
@group(0) @binding(5) var<storage,read> data:array<f32>;
@group(0) @binding(6) var<uniform> p:Params;
fn src(x:u32,y:u32)->f32 {return source[p.source.w+y*p.source.z+x].x;}
fn aux(x:u32,y:u32)->f32 {return auxiliary[p.auxiliary.w+y*p.auxiliary.z+x].x;}
fn even(v:f32)->f32 {let lo=floor(v);let t=v-lo;return lo+select(0.0,1.0,t>0.5||(t==0.5&&i32(lo)%2!=0));}
fn base_position(index:u32)->vec2<f32> {
    return fields[p.field.w+u32(p.args[0].x)*p.field.z+8u+index].xy*p.args[0].zw-p.args[1].xy;
}
fn position(index:u32)->vec2<f32> {
    let point=base_position(index);if p.output.z!=41u {return point;}
    var centers:array<vec2<f32>,2>;
    for(var eye=0u;eye<2u;eye++) {var center=vec2<f32>(0.0);for(var i=0u;i<16u;i++) {center+=base_position(u32(data[36u+eye*16u+i]));}centers[eye]=center/16.0;}
    let center=(centers[0]+centers[1])*.5;let distance=length(centers[1]-centers[0]);let axis=(centers[1]-centers[0])/max(distance,1e-6);
    var normal=vec2<f32>(-axis.y,axis.x);if dot(normal,base_position(152u)-center)<0.0 {normal=-normal;}
    let t=clamp((-dot(point-center,normal)/max(distance,1e-6)-.05)/.60,0.0,1.0);
    return point-.28*distance*t*t*(3.0-2.0*t)*normal;
}
fn on_line(q:vec2<i32>,aa:vec2<i32>,bb:vec2<i32>)->bool {
    var a=aa;var b=bb;if a.x>b.x||(a.x==b.x&&a.y>b.y) {a=bb;b=aa;}
    let d=b-a;let ad=abs(d);let major=max(ad.x,ad.y);if major==0 {return all(q==a);}
    let horizontal=ad.x>=ad.y;let k=select(abs(q.y-a.y),q.x-a.x,horizontal);
    if k<0||k>major||q.y<min(a.y,b.y)||q.y>max(a.y,b.y) {return false;}
    let minor=(2*min(ad.x,ad.y)*k+major-1)/(2*major);
    let point=a+select(vec2<i32>(minor,k*select(-1,1,d.y>=0)),vec2<i32>(k,minor*select(-1,1,d.y>=0)),horizontal);
    return all(q==point);
}
fn polygon(q:vec2<i32>,count:u32)->bool {
    var intersections:array<i32,80>;var n=0u;
    for(var i=0u;i<count;i++) {
        let aa=position(u32(data[i]));let bb=position(u32(data[(i+1u)%count]));
        var a=vec2<i32>(vec2<f32>(even(aa.x),even(aa.y)));var b=vec2<i32>(vec2<f32>(even(bb.x),even(bb.y)));
        if on_line(q,a,b) {return true;}
        if a.y>b.y {let t=a;a=b;b=t;}
        if q.y>=a.y&&q.y<b.y {
            let slope=((b.x-a.x)*65536)/(b.y-a.y);let crossing=a.x*65536+(q.y-a.y)*slope;
            var j=n;while j>0u&&intersections[j-1u]>crossing {intersections[j]=intersections[j-1u];j--;}
            intersections[j]=crossing;n++;
        }
    }
    for(var i=0u;i+1u<n;i+=2u) {if q.x>=(intersections[i]+65535)>>16 && q.x<=intersections[i+1u]>>16 {return true;}}
    return false;
}
fn sum(left:i32,right:i32,row:i32)->f32 {
    let first=max(0,left);let last=min(i32(p.output.x)-1,right);if first>last {return 0.0;}
    var value=aux(u32(last),u32(row));if first>0 {value-=aux(u32(first-1),u32(row));}return value;
}
fn has_zero(x:i32,row:i32,radius:i32)->bool {
    let lo=max(0,x-radius);let hi=min(i32(p.output.x)-1,x+radius);
    return sum(lo,hi,row)<f32(hi-lo+1);
}
fn chamfer(x:i32,y:i32)->f32 {
    let hi=max(abs(x),abs(y));let lo=min(abs(x),abs(y));
    if hi>=2*lo {return (f32(hi-2*lo)*65536.0+f32(lo)*143976.0)/65536.0;}
    return (f32(hi-lo)*143976.0+f32(2*lo-hi)*91750.0)/65536.0;
}
fn sine(degrees:i32)->f32 {
    let angle=((degrees%360)+360)%360;
    switch angle {case 0,180:{return 0.0;}case 90:{return 1.0;}case 270:{return -1.0;}default:{return sin(f32(angle)*0.017453292519943295);}}
}
fn fixed_line(q:vec2<i32>,first:vec2<i32>,second:vec2<i32>)->bool {
    let horizontal=abs(second.x-first.x)>abs(second.y-first.y);let axis=select(1u,0u,horizontal);let other=1u-axis;
    var a=first;var b=second;if a[axis]>b[axis] {a=second;b=first;}
    if all(q==((b+32768)>>vec2<u32>(16u))) {return true;}
    let steps=(b[axis]-a[axis])>>16;let k=q[axis]-((a[axis]+32768)>>16);
    if k<0||k>steps {return false;}
    let increment=i32(trunc(f32(b[other]-a[other])*65536.0/f32((b[axis]-a[axis])|1)));
    return q[other]==(a[other]+32768+k*increment)>>16;
}
fn convex_fixed(q:vec2<i32>,vertices:array<vec2<i32>,80>,count:u32)->bool {
    var v=vertices;var first=0u;var lowest=v[0].y;var highest=lowest;
    for(var i=0u;i<count;i++) {
        if fixed_line(q,v[i],v[(i+1u)%count]) {return true;}
        if v[i].y<lowest {lowest=v[i].y;first=i;}highest=max(highest,v[i].y);
    }
    if q.y<(lowest+32768)>>16||q.y>=(highest+32768)>>16 {return false;}
    var crossings:array<i32,2>;
    for(var side=0u;side<2u;side++) {
        let direction=select(1u,count-1u,side==1u);var a=first;var start=(lowest+32768)>>16;
        for(var k=0u;k<count;k++) {
            let b=(a+direction)%count;let end=(v[b].y+32768)>>16;
            if end>q.y {
                let delta=end-start;let increment=((v[b].x-v[a].x)*2+delta)/(2*delta);
                crossings[side]=v[a].x+(q.y-start)*increment;break;
            }
            a=b;start=end;
        }
    }
    let lo=(min(crossings[0],crossings[1])+32768)>>16;let hi=(max(crossings[0],crossings[1])+32768)>>16;
    return q.x>=lo&&q.x<=hi;
}
fn under_eye(q:vec2<f32>)->bool {
    var centers:array<vec2<f32>,2>;
    for(var eye=0u;eye<2u;eye++) {var center=vec2<f32>(0.0);for(var i=0u;i<16u;i++) {center+=position(u32(data[eye*16u+i]));}centers[eye]=center/16.0;}
    let distance=length(centers[1]-centers[0]);let direction=(centers[1]-centers[0])/max(distance,0.000001);
    var normal=vec2<f32>(-direction.y,direction.x);if dot(normal,position(152u)-(centers[0]+centers[1])*.5)<0.0 {normal=-normal;}
    let angle=i32(even(atan2(direction.y,direction.x)*57.29577951308232));let cosine=sine(90-angle);let sinus=sine(angle);
    for(var eye=0u;eye<2u;eye++) {
        let c=centers[eye]+normal*(.13*distance);let center=vec2<f32>(even(c.x),even(c.y));
        let width=length(position(u32(data[eye*16u]))-position(u32(data[eye*16u+8u])));
        let axes=vec2<f32>(max(1.0,even(width*.5)),max(1.0,even(distance*.11)));
        let step=select(select(select(5u,18u,max(axes.x,axes.y)<15.0),30u,max(axes.x,axes.y)<10.0),90u,max(axes.x,axes.y)<3.0);
        var vertices:array<vec2<i32>,80>;var n=0u;
        for(var degrees=0u;degrees<360u;degrees+=step) {
            let at=axes*vec2<f32>(sine(90-i32(degrees)),sine(i32(degrees)));
            let offset=vec2<f32>(cosine*at.x-sinus*at.y,sinus*at.x+cosine*at.y)*65536.0;
            vertices[n]=vec2<i32>(center)*65536+vec2<i32>(vec2<f32>(even(offset.x),even(offset.y)));n++;
        }
        if convex_fixed(vec2<i32>(q),vertices,n) {return true;}
    }
    return false;
}
@compute @workgroup_size(8,8)
fn main(@builtin(global_invocation_id) id:vec3<u32>) {
    if any(id.xy>=p.output.xy) {return;}let x=id.x;let y=id.y;let mode=p.output.z;var value=src(x,y);
    switch mode {
        case 32u,41u:{value=select(0.0,1.0,polygon(vec2<i32>(id.xy),u32(p.args[0].y)));}
        case 33u:{if x!=0u {return;}var total=0.0;for(var col=0u;col<p.output.x;col++) {total+=src(col,y);result[y*p.output.x+col]=vec4<f32>(total);}return;}
        case 34u:{
            let radius=i32(p.args[0].x);value=0.0;
            for(var dy=-radius;dy<=radius;dy++) {let row=i32(y)+dy;if row<0||row>=i32(p.output.y) {continue;}
                let dx=i32(even(sqrt(max(0.0,f32(radius*radius-dy*dy)))));
                if sum(i32(x)-dx,i32(x)+dx,row)>0.0 {value=1.0;break;}}
        }
        case 35u:{
            if value>0.0 {
                var best=p.args[0].x;let radius=i32(ceil(best));
                for(var dy=-radius;dy<=radius;dy++) {let row=i32(y)+dy;if row<0||row>=i32(p.output.y)||f32(abs(dy))>=best {continue;}
                    if !has_zero(i32(x),row,radius) {continue;}var lo=0;var hi=radius;
                    while lo<hi {let mid=(lo+hi)/2;if has_zero(i32(x),row,mid) {hi=mid;}else {lo=mid+1;}}
                    best=min(best,chamfer(lo,dy));}
                let t=clamp(best/p.args[0].x,0.0,1.0);value=select(t*t*(3.0-2.0*t),best,p.args[0].y!=0.0);
            }
        }
        case 36u:{value=select(value,1.0-value,p.args[0].x!=0.0);if x==0u||y==0u||x+1u==p.output.x||y+1u==p.output.y {value=0.0;}}
        case 37u:{value*=1.0-aux(x,y);}
        case 38u:{value*=extra[p.extra.w+y*p.extra.z+x].x;}
        case 39u:{value=max(value,aux(x,y));}
        default:{value=select(0.0,1.0,under_eye(vec2<f32>(id.xy)));}
    }
    result[y*p.output.x+x]=vec4<f32>(value);
}
