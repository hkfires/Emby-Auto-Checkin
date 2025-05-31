# Emby 自动签到工具

## 目前适配Emby

*   HKA Emby
*   ~~Beebi（比比）~~
*   YounoEmby
*   69云机场

## 使用 Docker 部署

您可以使用 Docker 和 Docker Compose 轻松部署此应用程序。

### 先决条件

*   [Docker](https://docs.docker.com/get-docker/)
*   [Docker Compose](https://docs.docker.com/compose/install/) (通常随 Docker 一起安装)

#### 官方一键脚本

```bash
bash <(curl -fsSL https://get.docker.com)
```

### 部署步骤

1.  **克隆或下载项目**

    如果您还没有项目文件，请先获取它们。

    ```bash
    git clone https://github.com/hkfires/Emby-Auto-Checkin.git
    ```

2.  **构建并运行容器**

    在项目的根目录 (包含 `Dockerfile` 和 `docker-compose.yml` 文件的目录) 中，打开终端并运行以下命令：

    ```bash
    docker compose up -d
    ```
    这个命令会：
    *   根据 `Dockerfile` 构建 Docker 镜像 (如果尚未构建)。
    *   在后台 (`-d`) 启动 `docker-compose.yml` 中定义的服务。

3.  **访问应用程序**

    一旦容器成功启动，您可以通过浏览器访问 `http://IP:5055` 来使用应用程序。

### 数据持久化

应用程序的数据 (例如配置文件、Telegram 会话文件) 将存储在项目根目录下的 `data` 文件夹中。这个文件夹会通过 Docker Compose 的卷挂载功能映射到容器内部的 `/app/data` 目录，从而确保即使容器停止或删除，数据也不会丢失。

### 停止容器

要停止应用程序，请在项目根目录的终端中运行：

```bash
docker compose down
```

### 查看日志

要查看容器的实时日志，可以运行：

```bash
docker compose logs -f
```

### 更新程序

进入项目根目录，执行以下代码

```bash
bash update.sh
```
