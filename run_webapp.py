import logging
from webapp import create_app

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = create_app()

if __name__ == '__main__':
    logger.info("启动Flask应用...")
    app.run(debug=True, host='0.0.0.0', port=5055, use_reloader=False)