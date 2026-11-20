import logging

# 每帧都会写的明细（资源记账、执行阶段、阶段交付、图像队列）用这个级别，默认不进日志文件；
# 排查时把根日志级别设成 TRACE 即可看到
TRACE = 5
logging.addLevelName(TRACE, "TRACE")
