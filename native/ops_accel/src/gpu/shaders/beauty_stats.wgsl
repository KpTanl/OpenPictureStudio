struct Params { output:vec4<u32>, source:vec4<u32>, auxiliary:vec4<u32>, field:vec4<u32>, extra:vec4<u32>, args:array<vec4<f32>,12> };
@group(0) @binding(0) var<storage,read> source:array<vec4<f32>>;
@group(0) @binding(1) var<storage,read> auxiliary:array<vec4<f32>>;
@group(0) @binding(2) var<storage,read> fields:array<vec4<f32>>;
@group(0) @binding(3) var<storage,read> extra:array<vec4<f32>>;
@group(0) @binding(4) var<storage,read_write> result:array<vec4<f32>>;
@group(0) @binding(5) var<storage,read> data:array<f32>;
@group(0) @binding(6) var<uniform> p:Params;
fn at(k:u32,s:vec4<u32>)->u32 {return s.w+(k/s.x)*s.z+k%s.x;}
fn unkey(k:u32)->f32 {return bitcast<f32>(select(~k,k^0x80000000u,(k&0x80000000u)!=0u));}
fn sample_count()->u32 {var count=0u;for(var i=0u;i<p.source.x*p.source.y;i++) {if source[at(i,p.source)].a>0.0 {count++;}}return count;}
// 中位数与离散度每行 3 格；只有一行时所有输出行共用它，多行时输出行与之一一对应
fn triple(row:u32,s:vec4<u32>)->u32 {return s.w+min(row,s.y-1u)*s.z;}
fn center(row:u32)->vec3<f32> {let o=triple(row,p.auxiliary);return vec3<f32>(auxiliary[o].x,auxiliary[o+1u].x,auxiliary[o+2u].x);}
@compute @workgroup_size(8,8)
fn main(@builtin(global_invocation_id) id:vec3<u32>) {
    if any(id.xy>=p.output.xy) {return;}let k=id.y*p.output.x+id.x;let mode=p.output.z;var value=vec4<f32>(0.0);
    switch mode {
        case 0u:{let first=k*1024u;let end=min(first+1024u,p.source.x*p.source.y);var count=0u;
            for(var i=first;i<end;i++) {if auxiliary[at(i,p.auxiliary)].x>0.0 {count++;}}value.x=f32(count);}
        case 1u:{var count=0.0;for(var i=0u;i<min(k,p.source.x*p.source.y);i++) {count+=source[at(i,p.source)].x;}
            if k==p.source.x*p.source.y {value=vec4<f32>(count,max(1.0,ceil(count/max(1.0,p.args[0].x))),0.0,0.0);}else {value.x=count;}}
        // 3 与 2 相同，但写出被选中像素的序号而不是像素值：序号只取决于蒙版，可以跨帧保留
        case 2u,3u:{
            let control=fields[at(p.field.x*p.field.y-1u,p.field)];let stride=u32(select(1.0,control.y,p.args[0].x!=0.0));let rank=k*stride;
            if f32(rank)<control.x {var block=0u;let blocks=p.field.x*p.field.y-1u;
                var lo=0u;var hi=blocks;while lo<hi {let mid=(lo+hi)/2u;if fields[at(mid,p.field)].x<=f32(rank) {lo=mid+1u;}else {hi=mid;}}block=lo-1u;
                var offset=u32(fields[at(block,p.field)].x);let end=min((block+1u)*1024u,p.source.x*p.source.y);
                for(var i=block*1024u;i<end;i++) {if auxiliary[at(i,p.auxiliary)].x>0.0 {if offset==rank {
                    if mode==3u {value=vec4<f32>(f32(i),0.0,0.0,1.0);}else {value=vec4<f32>(source[at(i,p.source)].rgb,1.0);}break;}offset++;}}
            }
        }
        case 4u:{let s=source[at(k,p.source)];value=vec4<f32>(abs(s.rgb-center(id.y)),s.a);}
        case 5u:{let s=source[at(k,p.source)];let o=triple(id.y,p.field);let spread=max(1.4826*vec3<f32>(fields[o].x,fields[o+1u].x,fields[o+2u].x),vec3<f32>(data[0],data[1],data[2]));
            value=vec4<f32>(s.rgb,select(0.0,s.a,all(abs(s.rgb-center(id.y))<p.args[0].x*spread)));}
        case 6u:{
            let c=center(0u);let n=sample_count();if k==0u {value=vec4<f32>(c,f32(n));}else if k<4u {var row=vec3<f32>(0.0);var correction=vec3<f32>(0.0);
                for(var i=0u;i<p.source.x*p.source.y;i++) {let s=source[at(i,p.source)];if s.a>0.0 {let d=s.rgb-c;let v=d[k-1u]*d;let next=row+v;correction+=select((v-next)+row,(row-next)+v,abs(row)>=abs(v));row=next;}}
                row=(row+correction)/max(1.0,f32(n));row[k-1u]+=select(9.0,36.0,k==1u);value=vec4<f32>(row,0.0);}
        }
        case 7u:{
            let first=source[p.source.w];let a=source[p.source.w+1u].rgb;let b=source[p.source.w+2u].rgb;let c=source[p.source.w+3u].rgb;
            let cof=mat3x3<f32>(cross(b,c),cross(c,a),cross(a,b));let determinant=dot(a,cross(b,c));
            if k==0u {value=first;}else if k<4u {value=vec4<f32>(vec3<f32>(cof[0][k-1u],cof[1][k-1u],cof[2][k-1u])/determinant,0.0);}else if k==4u {value.x=max(4.0,sqrt(a.x));}
        }
        case 8u:{let n=sample_count();let c=center(0u);value=vec4<f32>(c,select(0.0,min(1.0,f32(n)/max(8.0,.008*p.args[0].x*p.args[0].x)),n>=8u));}
        case 9u:{value=vec4<f32>(source[at(k,p.source)].rgb,extra[at(k,p.extra)].x);}
        // 8 的按行版本：第 k 个输出对应样本表的第 k 行
        case 10u:{var n=0u;for(var i=0u;i<p.source.x;i++) {if source[p.source.w+k*p.source.z+i].a>0.0 {n++;}}
            let c=center(k);value=vec4<f32>(c,select(0.0,min(1.0,f32(n)/max(8.0,.008*p.args[0].x*p.args[0].x)),n>=8u));}
        case 11u:{
            let channel=id.y;var state=fields[p.field.w+channel*p.field.z];var counts=vec2<f32>(0.0);
            for(var i=0u;i<p.source.x;i++) {counts+=source[p.source.w+channel*p.source.z+i].xy;}
            let mask=1u<<u32(p.args[0].x);if state.y>=counts.x {state.x=bitcast<f32>(bitcast<u32>(state.x)|mask);state.y-=counts.x;}
            if state.w>=counts.y {state.z=bitcast<f32>(bitcast<u32>(state.z)|mask);state.w-=counts.y;}value=state;
        }
        case 12u:{let first=k*1024u;let end=min(first+1024u,p.source.x*p.source.y);var count=0u;for(var i=first;i<end;i++) {if source[at(i,p.source)].a>0.0 {count++;}}value.x=f32(count);}
        case 13u:{let count=source[p.source.w].x+source[p.source.w].y*65536.0;value=vec4<f32>(0.0,floor(max(0.0,count-1.0)/2.0),0.0,floor(count/2.0));}
        case 14u:{let state=source[p.source.w+id.x*p.source.z];let count=fields[p.field.w].x+fields[p.field.w].y*65536.0;if count>0.0 {value=vec4<f32>((unkey(bitcast<u32>(state.x))+unkey(bitcast<u32>(state.z)))*.5,0.0,0.0,count);}}
        case 15u:{var total=0u;for(var i=0u;i<p.source.x*p.source.y;i++) {total+=u32(source[at(i,p.source)].x);}value=vec4<f32>(f32(total&65535u),f32(total>>16u),0.0,0.0);}
        // 按序号表取样：表里每格是 3 写出的像素序号
        case 16u:{let cell=auxiliary[at(k,p.auxiliary)];if cell.a>0.0 {value=vec4<f32>(source[at(u32(cell.x),p.source)].rgb,1.0);}}
        default:{value=source[at(k,p.source)]+auxiliary[at(k,p.auxiliary)]*p.args[0].x+fields[at(k,p.field)]*p.args[0].y+extra[at(k,p.extra)]*p.args[0].z;}
    }
    result[k]=value;
}
