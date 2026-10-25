import os
import logging

from .constants import RAW_EXTENSIONS
from .raw_loader import load_image


logger = logging.getLogger(__name__)

class PDFExporter:
    """
    负责将图片导出为 PDF 的核心逻辑，优化了内存释放。
    """
    def __init__(self):
        self.use_img2pdf = False
        try:
            import img2pdf
            self.use_img2pdf = True
        except ImportError:
            pass

    def export(self, input_paths: list[str], output_path: str) -> bool:
        """
        执行导出动作，分批处理并显式关闭句柄以优化内存。
        """
        if not input_paths:
            return False

        try:
            has_raw_input = any(os.path.splitext(p)[1].lower() in RAW_EXTENSIONS for p in input_paths)

            if self.use_img2pdf and not has_raw_input:
                import img2pdf
                # img2pdf 本身是流式处理，通常对内存友好
                with open(output_path, "wb") as f:
                    f.write(img2pdf.convert(input_paths))
                return True
            else:
                # 优化 PIL 导出逻辑
                # RAW 输入需先解码为 PIL 图像后再导出。
                first_img = load_image(input_paths[0])
                if first_img.mode != 'RGB':
                    first_img = first_img.convert('RGB')
                
                # 准备剩余图片
                other_imgs = []
                for p in input_paths[1:]:
                    img = load_image(p)
                    if img.mode != 'RGB':
                        img = img.convert('RGB')
                    other_imgs.append(img)
                
                first_img.save(output_path, save_all=True, append_images=other_imgs)
                
                # 显式关闭所有句柄
                first_img.close()
                for img in other_imgs:
                    img.close()
                    
                return True
        except Exception:
            logger.exception("PDF export failed: %s", output_path)
            return False
