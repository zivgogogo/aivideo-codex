"""Local video validation; uploads do not send media to a model provider."""
import re
import subprocess
import imageio_ffmpeg

VIDEO_MAX_BYTES=100*1024*1024


def video_info(path):
    try:
        result=subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(),'-hide_banner','-nostdin','-v','info','-i',str(path),
                               '-map','0:v:0','-frames:v','1','-an','-f','null','-'],capture_output=True,text=True,errors='replace',timeout=25)
    except subprocess.TimeoutExpired:raise ValueError('视频读取超时，请使用可播放的MP4/MOV文件') from None
    source=result.stderr.split('Output #',1)[0]
    stream=next((line for line in source.splitlines() if 'Video:' in line),'')
    size=re.search(r'(?<!\w)(\d{2,5})x(\d{2,5})(?!\w)',stream)
    duration=re.search(r'Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)',source)
    if result.returncode or not size or not duration:raise ValueError('无法读取视频，请上传完整、可播放的MP4/MOV文件')
    width,height=map(int,size.groups());seconds=int(duration[1])*3600+int(duration[2])*60+float(duration[3])
    if not 1<=seconds<=30:raise ValueError('参考视频时长须为1~30秒')
    if not all(240<=n<=4096 for n in (width,height)) or not 1/8<=width/height<=8:
        raise ValueError('参考视频宽高须为240~4096像素，宽高比1:8~8:1')
    return dict(width=width,height=height,duration=seconds,bytes=path.stat().st_size)
