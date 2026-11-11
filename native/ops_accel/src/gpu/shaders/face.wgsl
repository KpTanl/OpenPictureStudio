struct Params { output:vec4<u32>, source:vec4<u32>, auxiliary:vec4<u32>, reserved:vec4<u32>, args:array<vec4<f32>,8> };
@group(0) @binding(0) var<storage,read> source:array<vec4<f32>>;
@group(0) @binding(1) var<storage,read> auxiliary:array<vec4<f32>>;
@group(0) @binding(2) var<storage,read> a:array<f32>;
@group(0) @binding(3) var<storage,read_write> result:array<vec4<f32>>;
@group(0) @binding(4) var<storage,read> data:array<f32>;
@group(0) @binding(5) var<uniform> p:Params;
const HEADER:u32=8u;
const POINTS:u32=478u;
const ROW:u32=486u;
fn src(x:u32,y:u32)->vec4<f32> { return source[p.source.w+y*p.source.z+x]; }
fn aux(x:u32,y:u32)->vec4<f32> { return auxiliary[p.auxiliary.w+y*p.auxiliary.z+x]; }
fn finite(v:f32)->bool { return v==v && abs(v)<3.4e38; }
fn even(v:f32)->f32 { let x=floor(v); let t=v-x; return x+select(0.0,1.0,t>0.5||(t==0.5&&i32(x)%2!=0)); }
fn point(x:i32,y:i32,zero:bool)->vec4<f32> {
    if zero&&(x<0||y<0||x>=i32(p.source.x)||y>=i32(p.source.y)) { return vec4<f32>(0.0,0.0,0.0,1.0); }
    return src(u32(clamp(x,0,i32(p.source.x)-1)),u32(clamp(y,0,i32(p.source.y)-1)));
}
fn sample(uv:vec2<f32>,zero:bool)->vec4<f32> {
    let pos=vec2<f32>(even(uv.x*32.0),even(uv.y*32.0))/32.0;
    let q=vec2<i32>(floor(pos)); let t=fract(pos);
    return (point(q.x,q.y,zero)*(1.0-t.x)+point(q.x+1,q.y,zero)*t.x)*(1.0-t.y)
        +(point(q.x,q.y+1,zero)*(1.0-t.x)+point(q.x+1,q.y+1,zero)*t.x)*t.y;
}
fn overlap(v:vec4<f32>,w:vec4<f32>)->f32 {
    let extent=max(min(v.zw,w.zw)-max(v.xy,w.xy),vec2<f32>(0.0));let area=extent.x*extent.y;
    let combined=(v.z-v.x)*(v.w-v.y)+(w.z-w.x)*(w.w-w.y)-area;
    return select(0.0,area/combined,combined>0.0);
}
fn same_face(row:u32,other:u32,other_input:bool)->bool {
    let first=src(1u,row); var second=src(1u,other); if other_input {second=aux(1u,other);}
    if overlap(first,second)<p.args[0].x {return false;}
    var sum=0.0;
    for(var i=0u;i<5u;i++) {
        let index=HEADER+u32(data[i]);let one=src(index,row).xy;var two=src(index,other).xy;if other_input {two=aux(index,other).xy;}
        let d=(one-two)*p.args[1].xy;sum+=dot(d,d);
    }
    var distance=aux(5u,other).x;if !other_input {distance=src(5u,other).x;}
    return sqrt(sum/5.0)<=p.args[0].y*min(src(5u,row).x,distance);
}
fn priority(first:u32,second:u32,other_input:bool)->bool {
    let v=src(2u,first);var w=src(2u,second);if other_input {w=aux(2u,second);}
    let c=src(3u,first);var d=src(3u,second);if other_input {d=aux(3u,second);}
    if (v.x==1.0)!=(w.x==1.0) {return v.x==1.0;}
    if v.y!=w.y {return v.y>w.y;}if v.z!=w.z {return v.z>w.z;}if v.w!=w.w {return v.w>w.w;}
    if c.w!=d.w {return c.w<d.w;}
    var f=src(4u,second).x;if other_input {f=aux(4u,second).x;}
    return src(4u,first).x<f;
}
fn order(first:u32,second:u32,kind:u32)->bool {
    if kind==3u {return src(4u,first).x<src(4u,second).x||(src(4u,first).x==src(4u,second).x&&first<second);}
    let v=src(2u,first);let w=src(2u,second);let c=src(3u,first);let d=src(3u,second);
    if kind!=0u {
        if (v.x==1.0)!=(w.x==1.0) {return v.x==1.0;}
        if kind==1u&&v.y!=w.y {return v.y>w.y;}
        if c.z!=d.z {return c.z>d.z;}
    }
    if c.x!=d.x {return c.x<d.x;}if c.y!=d.y {return c.y<d.y;}
    if kind==0u&&c.z!=d.z {return c.z<d.z;}
    return first<second;
}
fn nms() {
    let hard=p.args[0].x!=0.0;let count=p.source.y;var used:array<u32,896>;
    let limit=select(p.output.y,u32(p.args[0].w),hard);
    for(var k=0u;k<p.output.x*p.output.y;k++) {result[k]=vec4<f32>(0.0);}
    if hard {var candidates=0u;for(var i=0u;i<count;i++) {if src(2u,i).x>0.0 {candidates++;}}
        if candidates>5000u {result[1u].w=-1.0;return;}}
    var selected=0u;var row=0u;var work=0u;
    while row<limit&&selected<5000u {
        var best=0.0;var index=count;
        for(var i=0u;i<count;i++) {
            work++;if hard&&work>2000000u {result[1u].w=-2.0;return;}
            let score=src(2u,i).x;if score<=best {continue;}
            if !hard&&used[i]!=0u {continue;}
            var suppressed=false;
            if hard {for(var j=0u;j<selected;j++) {work++;if work>2000000u {result[1u].w=-2.0;return;}
                let previous=u32(result[(limit+j)*2u].x);if overlap(src(0u,i),src(0u,previous))>p.args[0].y {suppressed=true;break;}}}
            if !suppressed {best=score;index=i;}
        }
        if index==count {break;}
        if hard {result[(limit+selected)*2u].x=f32(index);}selected++;let box=src(0u,index);var bounds=box;var eyes=src(1u,index);
        if !hard {
            var total=0.0;var combined=vec4<f32>(0.0);var keypoints=vec4<f32>(0.0);
            for(var i=0u;i<count;i++) {
                let score=src(2u,i).x;
                if used[i]==0u&&score>0.0&&overlap(box,src(0u,i))>p.args[0].y {
                    total+=score;combined+=score*src(0u,i);keypoints+=score*src(1u,i);used[i]=1u;
                }
            }
            used[index]=1u;if total>0.0 {bounds=combined/total;eyes=keypoints/total;}
        }
        let center=(bounds.xy+bounds.zw)*0.5;let extent=bounds.zw-bounds.xy;
        if hard&&max(extent.x*p.args[1].x,extent.y*p.args[1].y)<p.args[0].z {continue;}
        let direction=(eyes.zw-eyes.xy)*p.args[1].xy;let angle=atan2(direction.y,direction.x);
        result[row*2u]=vec4<f32>(center,extent*select(1.5,1.0,hard));
        result[row*2u+1u]=vec4<f32>(angle,best,1.0,0.0);
        row++;
    }
}
fn merge() {
    for(var k=0u;k<p.output.x*p.output.y;k++) {result[k]=src(k%ROW,k/ROW);}
    result[7u].x+=aux(4u,0u).z;
    if aux(4u,0u).y==0.0 {return;}
    var matching:array<u32,50>;var best=p.source.y;var slot=p.source.y;
    for(var row=0u;row<p.source.y;row++) {
        if src(4u,row).y==0.0 {slot=min(slot,row);continue;}
        if p.args[0].z!=0.0&&same_face(row,0u,true) {
            matching[row]=1u;slot=min(slot,row);
            if p.args[0].z==2.0 {return;}
            if best==p.source.y||priority(row,best,false) {best=row;}
        }
    }
    var use_new=best==p.source.y;if !use_new {use_new=!priority(best,0u,true);}
    if slot==p.source.y {
        slot=0u;for(var row=1u;row<p.source.y;row++) {if order(slot,row,2u) {slot=row;}}
        if !use_new {return;}
        let one=aux(2u,0u);let two=src(2u,slot);let c=aux(3u,0u);let d=src(3u,slot);
        if (two.x==1.0&&one.x!=1.0)||((two.x==1.0)==(one.x==1.0)&&d.z>c.z) {return;}
    }
    for(var row=0u;row<p.source.y;row++) {if matching[row]!=0u {result[row*ROW+4u].y=0.0;}}
    for(var col=0u;col<ROW;col++) {result[slot*ROW+col]=select(src(col,best),aux(col,0u),use_new);}
}
@compute @workgroup_size(8,8)
fn main(@builtin(global_invocation_id) id:vec3<u32>) {
    if any(id.xy>=p.output.xy) {return;}let x=id.x;let y=id.y;let mode=p.output.z;
    if mode==3u {if x==0u&&y==0u {nms();}return;}
    if mode==7u {if x==0u&&y==0u {merge();}return;}
    var value=vec4<f32>(0.0);
    switch mode {
        case 0u: {
            let bounds=p.args[0];let scale=(bounds.zw-bounds.xy)/vec2<f32>(p.output.xy);
            let lo=bounds.xy+vec2<f32>(id.xy)*scale;let hi=lo+scale;var total=vec3<f32>(0.0);
            for(var row=i32(floor(lo.y));row<i32(ceil(hi.y));row++) {for(var col=i32(floor(lo.x));col<i32(ceil(hi.x));col++) {
                let weight=(min(hi.x,f32(col+1))-max(lo.x,f32(col)))*(min(hi.y,f32(row+1))-max(lo.y,f32(row)));
                let pixel=point(col,row,false);total+=(pixel.rgb*pixel.a+(1.0-pixel.a))*weight;
            }}
            let linear=clamp(total/(scale.x*scale.y),vec3<f32>(0.0),vec3<f32>(1.0));
            let encoded=select(1.055*pow(linear,vec3<f32>(1.0/2.4))-0.055,linear*12.92,linear<=vec3<f32>(0.0031308));
            value=vec4<f32>(even(encoded.r*255.0)/255.0,even(encoded.g*255.0)/255.0,even(encoded.b*255.0)/255.0,1.0);
        }
        case 1u: {
            let side=f32(max(p.source.x,p.source.y));
            let uv=vec2<f32>(id.xy)/vec2<f32>(p.output.xy)*side-(side-vec2<f32>(p.source.xy))*0.5;
            value=sample(uv,true);
            value=vec4<f32>(floor(value.rgb*255.0+0.5)/255.0,1.0);
            if p.args[0].x==1.0 {value=point(i32(x),i32(y),true);}
        }
        case 2u: {
            let index=y;let fine=index<512u;let cell=select((index-512u)/6u,index/2u,fine);let size=select(8u,16u,fine);
            let anchor=(vec2<f32>(f32(cell%size),f32(cell/size))+0.5)/f32(size);
            let offset=index*16u;let center=vec2<f32>(a[offset],a[offset+1u])/128.0+anchor;
            let extent=vec2<f32>(a[offset+2u],a[offset+3u])/128.0;
            let scale=f32(max(p.source.x,p.source.y))/vec2<f32>(p.source.xy);let pad=(scale-1.0)*0.5;
            let score=1.0/(1.0+exp(-clamp(a[14336u+index],-100.0,100.0)));
            if x==0u {value=vec4<f32>((center-extent*0.5)*scale-pad,(center+extent*0.5)*scale-pad);}
            else if x==1u {value=vec4<f32>((vec2<f32>(a[offset+4u],a[offset+5u])/128.0+anchor)*scale-pad,(vec2<f32>(a[offset+6u],a[offset+7u])/128.0+anchor)*scale-pad);}
            else if x==2u {value=vec4<f32>(select(0.0,score,score>=p.args[0].x&&all(extent>vec2<f32>(0.0))&&finite(score)),0.0,0.0,0.0);}
        }
        case 4u: {
            let face=u32(p.args[0].x);let rect=aux(0u,face);let flags=aux(1u,face);
            if flags.z!=0.0 {
                let local=(vec2<f32>(id.xy)/vec2<f32>(p.output.xy)-0.5)*rect.zw*vec2<f32>(p.source.xy);
                let rotated=vec2<f32>(cos(flags.x)*local.x-sin(flags.x)*local.y,sin(flags.x)*local.x+cos(flags.x)*local.y);
                value=sample(rect.xy*vec2<f32>(p.source.xy)+rotated,false);
                value=vec4<f32>(floor(value.rgb*255.0+0.5)/255.0,1.0);
            }
        }
        case 5u: {
            let face=u32(p.args[0].x);let rect=aux(0u,face);let flags=aux(1u,face);let point0=vec2<f32>(a[x*3u],a[x*3u+1u])/256.0-0.5;
            let local=point0*rect.zw*p.args[2].xy;
            let rotated=vec2<f32>(cos(flags.x)*local.x-sin(flags.x)*local.y,sin(flags.x)*local.x+cos(flags.x)*local.y)/p.args[2].xy;
            let position=(rect.xy+rotated)*p.args[1].zw+p.args[1].xy;
            let valid=flags.z!=0.0&&a[1434u]>0.0&&finite(position.x)&&finite(position.y);
            value=vec4<f32>(position,a[x*3u+2u]/256.0,select(0.0,1.0,valid));
        }
        case 6u: {
            if x>=HEADER {value=src(x-HEADER,0u);break;}
            var low=vec2<f32>(3.4e38);var high=vec2<f32>(-3.4e38);var mean=vec2<f32>(0.0);var valid=true;
            for(var i=0u;i<POINTS;i++) {let q=src(i,0u);valid=valid&&q.w!=0.0;low=min(low,q.xy);high=max(high,q.xy);mean+=q.xy;}
            var oval_low=vec2<f32>(3.4e38);var oval_high=vec2<f32>(-3.4e38);var area=0.0;
            for(var i=0u;i<36u;i++) {let q=src(u32(data[i]),0u).xy;let next=src(u32(data[(i+1u)%36u]),0u).xy;
                oval_low=min(oval_low,q);oval_high=max(oval_high,q);let first=q*p.args[2].zw;let second=next*p.args[2].zw;area+=first.x*second.y-first.y*second.x;}
            var one=vec2<f32>(0.0);var two=vec2<f32>(0.0);
            for(var i=0u;i<16u;i++) {one+=src(u32(data[36u+i]),0u).xy;two+=src(u32(data[52u+i]),0u).xy;}
            let difference=(one-two)/16.0;let distance=length(difference*p.args[2].zw);
            let window_distance=length(difference/p.args[1].zw*p.args[3].xy);
            var status=1.0;if distance<8.0 {status=2.0;}
            if distance>=8.0&&(abs(area)*0.5<distance*distance*0.1||any(high<=vec2<f32>(0.0))||any(low>=vec2<f32>(1.0))) {status=3.0;}
            let relative_low=(oval_low-p.args[1].xy)/p.args[1].zw;let relative_high=(oval_high-p.args[1].xy)/p.args[1].zw;
            var margin=1.0;
            if p.args[1].x>0.0 {margin=min(margin,relative_low.x);}if p.args[1].y>0.0 {margin=min(margin,relative_low.y);}
            if p.args[1].x+p.args[1].z<1.0 {margin=min(margin,1.0-relative_high.x);}if p.args[1].y+p.args[1].w<1.0 {margin=min(margin,1.0-relative_high.y);}
            if p.args[0].x!=0.0&&margin<p.args[0].z {valid=false;}
            switch x {
                case 0u:{value=vec4<f32>(low,high);}case 1u:{value=vec4<f32>(oval_low,oval_high);}
                case 2u:{value=vec4<f32>(status,p.args[0].w,margin,window_distance);}
                case 3u:{value=vec4<f32>(mean/478.0,(high.x-low.x)*(high.y-low.y),p.args[0].x);}
                case 4u:{value=vec4<f32>(p.args[0].y,select(0.0,1.0,valid),src(0u,0u).w,0.0);}
                case 5u:{value=vec4<f32>(distance,one/16.0,0.0);}
                case 6u:{let dist=length(difference*p.args[2].xy);let pad=dist*0.3;value=vec4<f32>(max(floor(low*p.args[2].xy-pad),vec2<f32>(0.0)),min(ceil(high*p.args[2].xy+pad+1.0),p.args[2].xy));}
                default:{value=vec4<f32>(0.0);}
            }
        }
        case 8u: {
            for(var row=0u;row<p.source.y;row++) {
                if src(4u,row).y==0.0 {continue;}var rank=0u;
                for(var other=0u;other<p.source.y;other++) {if src(4u,other).y!=0.0&&order(other,row,u32(p.args[0].x)) {rank++;}}
                if rank==y {value=src(x,row);break;}
            }
            if x==7u&&y==0u {value.x=src(7u,0u).x;}
        }
        case 9u: {
            let flags=src(4u,y);let box=src(0u,y);let size=p.args[0].xy;let center=(box.xy+box.zw)*0.5*size;
            let half=(box.zw-box.xy)*size*(p.args[0].z*0.5);
            if x==0u {value=vec4<f32>(max(vec2<f32>(0.0),floor(center-half)),min(size,ceil(center+half)));}
            else if x==1u {value=vec4<f32>(flags.y,src(2u,y).x,src(2u,y).y,0.0);}
        }
        case 10u: {
            let row=u32(p.args[0].z);value=src(x,row);
            if x==4u&&value.y!=0.0&&src(2u,row).y==0.0&&src(2u,row).x==1.0 {
                var matched=false;for(var face=0u;face<p.auxiliary.y;face++) {if aux(4u,face).y!=0.0&&aux(2u,face).x==1.0&&same_face(row,face,true) {matched=true;break;}}
                if !matched {value.y=0.0;}
            }
        }
        case 11u: {
            for(var row=0u;row<p.source.y;row++) {if src(4u,row).y!=0.0 {value.y+=1.0;if src(2u,row).x==1.0 {value.x+=1.0;}}}
        }
        case 12u: {
            let row=y;let n8=u32(p.args[0].x);let n16=u32(p.args[0].y);var level=0u;var index=row;
            if row>=n8+n16 {level=2u;index=row-n8-n16;}else if row>=n8 {level=1u;index=row-n8;}
            let stride=8u<<level;let grid=p.source.x/stride;let cell=vec2<f32>(f32(index%grid),f32(index/grid));
            let score=sqrt(clamp(a[u32(data[level])+index],0.0,1.0)*clamp(a[u32(data[level+3u])+index],0.0,1.0));
            let at=u32(data[level+6u])+index*4u;let center=(cell+vec2<f32>(a[at],a[at+1u]))*f32(stride);
            let extent=exp(vec2<f32>(a[at+2u],a[at+3u]))*f32(stride);let size=p.args[1].xy;
            if x==0u {value=vec4<f32>((center-extent*.5)/size,(center+extent*.5)/size);}
            else if x==1u {let k=u32(data[level+9u])+index*10u;value=vec4<f32>((cell+vec2<f32>(a[k],a[k+1u]))*f32(stride)/size,(cell+vec2<f32>(a[k+2u],a[k+3u]))*f32(stride)/size);}
            else if x==2u {value=vec4<f32>(select(0.0,score,score>=p.args[0].z&&finite(score)&&all(extent>vec2<f32>(0.0))),0.0,0.0,0.0);}
        }
        case 13u: {value=vec4<f32>(0.0);if p.args[0].x!=0.0&&x==7u&&y==0u {value=src(7u,0u);}}
        case 14u: {
            let row=u32(p.args[0].x);let rect=src(0u,row);let center=rect.xy;let extent=rect.zw;
            var covered=false;
            for(var face=0u;face<p.auxiliary.y;face++) {let oval=aux(1u,face);let c=(oval.xy+oval.zw)*0.5;
                if aux(4u,face).y!=0.0&&all(c>=center-extent*.5)&&all(c<=center+extent*.5) {covered=true;break;}}
            let half=max(extent.x*p.args[1].x,extent.y*p.args[1].y)*p.args[0].y*.5;
            value=vec4<f32>(max(vec2<f32>(0.0),floor(center*p.args[1].xy-half)),min(p.args[1].xy,ceil(center*p.args[1].xy+half)));
            if x==1u {value=vec4<f32>(select(1.0,0.0,covered),src(1u,row).z,src(1u,0u).w,0.0);}
        }
        case 16u: {
            let row=u32(p.args[0].x);let size=p.args[1].xy;let bounds=src(0u,row);
            var one=vec2<f32>(0.0);var two=vec2<f32>(0.0);
            for(var i=0u;i<16u;i++) {one+=src(HEADER+u32(data[i]),row).xy;two+=src(HEADER+u32(data[i+16u]),row).xy;}
            let distance=length((one-two)/16.0*size);let pad=distance*select(.3,p.args[0].y,p.args[0].y>0.0);
            value=vec4<f32>(max(vec2<f32>(0.0),floor(bounds.xy*size-pad)),min(size,ceil(bounds.zw*size+pad+1.0)));
            if x==1u {value=vec4<f32>(distance,max(1.0,.03*distance),max(1.0,even(.02*distance)),max(1.0,even(.20*distance)));}
        }
        case 17u: {value=src(4u,y);if x==1u {value=vec4<f32>(src(2u,y).x,src(7u,0u).x,0.0,0.0);}}
        case 18u: {value=vec4<f32>(src(1u,y).z,0.0,0.0,0.0);}
        default: {
            value=src(x,y);if x==4u {let oval=src(1u,y);let center=(oval.xy+oval.zw)*.5;
                let rect=aux(0u,u32(p.args[0].x));
                if any(center<rect.xy-rect.zw*.5)||any(center>rect.xy+rect.zw*.5) {value.y=0.0;}}
        }
    }
    result[y*p.output.x+x]=value;
}
