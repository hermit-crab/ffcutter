### ffcutter
Command line + gui utility to cut videos using ffmpeg.

![Screenshot](http://i.imgur.com/IwVuoMG.png)

### Installation
##### Linux
```
> apt-get install ffmpeg mpv python3-pyqt5 python3-docopt python3-colorama
or
> yum install ffmpeg mpv python3-pyqt5 python3-docopt python3-colorama
or
> pacman -S ffmpeg mpv python-pyqt5 python-docopt python-colorama
then
> git clone https://github.com/Unknowny/ffcutter.git
```
Python dependencies can also be installed with `pip install pyqt5 docopt colorama`.  
FFmpeg may not be available in default repositories. Refer to the [ffmpeg download page](https://ffmpeg.org/download.html#build-linux).

##### Windows
Latest all included x86_64 only archive is available in [releases](https://github.com/Unknowny/ffcutter/releases).  
Run ffcutter.exe from terminal or drop video file into it. Press H to print help message to the terminal.  
If you encounter ucrtbase.terminate error install this windows update - [KB2999226](https://www.microsoft.com/en-us/download/details.aspx?id=49093).

Alternatively you can install all the dependacies manually, then place ffmpeg.exe, ffprobe.exe, [mpv-1.dll](https://github.com/Unknowny/ffcutter/blob/master/win/mpv-1.dll), [D3DCompiler_43.dll](https://github.com/Unknowny/ffcutter/blob/master/win/D3DCompiler_43.dll) next to ffcutter.py. 


### Usage
```
> ffcutter.py <video-file> [-s <save-file> --mpv=mpv-option...]
> ffcutter.py -h | --help
```
