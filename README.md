# Emby 自动签到工具

一个支持多用户、多机器人、多策略的 Telegram 自动签到工具，特别适配了多个 Emby 社区的签到机器人。

## 主要功能

*   **Web 界面**：提供简单易用的网页来进行所有配置。
*   **多用户管理**：支持多个 Telegram 账号同时运行。
*   **定时任务**：可自定义每个签到任务的执行时间。
*   **多种签到策略**：内置多种策略以适应不同机器人的签到方式。
*   **图片验证码识别**：支持接入大语言模型（LLM）的 Vision API 来自动识别图片验证码。

## 签到策略说明

您可以根据不同机器人或频道的签到要求，在创建任务时选择合适的策略：

| 策略名称 | 描述 |
| :--- | :--- |
| **发送签到指令** | 最简单的策略，直接向机器人发送指定的命令（如 `/checkin`）。 |
| **点击签到按钮** | 发送 `/start` 后，点击响应消息中的“签到”按钮，适用于大部分简单签到机器人。 |
| **签到按钮+验证** | 点击签到按钮后，需要完成数学计算题验证。 |
| **checkin+图片识别** | 发送 `/checkin` 后，需要识别图片验证码并点击对应选项。**需要配置LLM API**。 |
| **发送自定义消息** | 向指定的群组或频道发送自定义内容，通常用于“冒泡”或发言任务。 |

## 已适配的 Emby 社群

本工具通过支持以下社群的签到机器人来完成签到。请根据不同机器人的要求，在任务设置中选择合适的签到策略。

*   HKA Emby
*   Beebi（比比）
*   YounoEmby
*   69云机场
*   终点站

## 部署指南

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
    ```bash
    git clone https://github.com/hkfires/Emby-Auto-Checkin.git
    cd Emby-Auto-Checkin
    ```

2.  **构建并运行容器**
    ```bash
    docker compose up -d
    ```
    这个命令会构建并启动容器。

3.  **访问应用程序**
    一旦容器成功启动，您可以通过浏览器访问 `http://服务器IP:5055` 来使用应用程序。

### 数据持久化

应用程序的数据（如配置文件、数据库、Telegram 会话文件）将存储在项目根目录下的 `data` 文件夹中，实现了数据持久化。

## 日常维护

### 停止容器

```bash
docker compose down
```

### 查看日志

```bash
docker compose logs -f
```

### 更新程序

```bash
bash update.sh
