# 使用官方 Python 运行时作为父镜像
FROM python:3.9-slim

# 设置工作目录
WORKDIR /app

# 将依赖文件复制到工作目录
COPY requirements.txt .

# 设置时区
ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# 安装依赖
RUN pip install --no-cache-dir -r requirements.txt

# 将项目中的所有文件复制到工作目录
COPY . .

# 暴露应用程序运行的端口
EXPOSE 5055

# 定义容器启动时运行的命令
CMD ["gunicorn", "-w", "1", "-b", "0.0.0.0:5055", "app:app"]
