import pygame
import edge_tts
import tempfile
import os
import time

class SoundEffectPlayer:
    def __init__(self):
        """初始化音效播放器"""
        pygame.mixer.init()
        self.sounds = {}
        self.playing_sounds = []  # 跟踪正在播放的声音
        print("音效播放器初始化成功")
    
    def load_sound(self, name, file_path):
        """加载音效"""
        try:
            self.sounds[name] = pygame.mixer.Sound(file_path)
            print(f"音效 '{name}' 加载成功: {file_path}")
        except Exception as e:
            print(f"音效 '{name}' 加载失败: {e}")
    
    def play_sound(self, name, wait=False):
        """播放音效
        
        Args:
            name: 音效名称
            wait: 是否等待播放完成
        """
        if name in self.sounds:
            channel = self.sounds[name].play()
            if wait and channel:
                # 等待播放完成
                while channel.get_busy():
                    time.sleep(0.1)
            print(f"音效 '{name}' 开始播放")
        else:
            print(f"音效 '{name}' 不存在")
    
    def stop_all_sounds(self):
        """停止所有音效"""
        pygame.mixer.stop()
        print("所有音效已停止")
    
    def cleanup(self):
        """清理资源"""
        self.stop_all_sounds()
        pygame.mixer.quit()
        print("音效播放器已关闭")

async def text_to_speech(text, voice="zh-CN-XiaoxiaoNeural"):
    """将文本转换为语音并返回临时文件路径"""
    audio_data = bytearray()
    communicate = edge_tts.Communicate(text, voice)
    
    print(f"正在生成语音: {text}")
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            audio_data.extend(chunk["data"])
    
    # 写入临时文件
    with tempfile.NamedTemporaryFile(suffix='.mp3', delete=False) as f:
        f.write(audio_data)
        tmp_path = f.name
    
    print(f"语音文件已生成: {tmp_path}")
    return tmp_path

async def Read(text, voice="zh-CN-XiaoxiaoNeural"):
    """主函数：文字转语音并播放"""
    tmp_path = None
    try:
        # 1. 生成语音文件
        tmp_path = await text_to_speech(text, voice)
        
        # 2. 确保播放器存在
        global sound_player
        if 'sound_player' not in globals():
            sound_player = SoundEffectPlayer()
        
        # 3. 加载并播放
        sound_name = "tts_voice"
        sound_player.load_sound(sound_name, tmp_path)
        
        # 4. 播放并等待完成
        print("开始播放语音...")
        sound_player.play_sound(sound_name, wait=True)
        print("语音播放完成")
        
    except Exception as e:
        print(f"播放过程中出错: {e}")
    
    finally:
        # 5. 清理临时文件
        if tmp_path and os.path.exists(tmp_path):
            try:
                # 等待一下确保文件已释放
                time.sleep(0.5)
                os.unlink(tmp_path)
                print(f"临时文件已删除: {tmp_path}")
            except PermissionError:
                print(f"临时文件删除失败，将在程序退出时清理: {tmp_path}")
                # 可以注册程序退出时的清理
                import atexit
                atexit.register(lambda: os.path.exists(tmp_path) and os.unlink(tmp_path))
