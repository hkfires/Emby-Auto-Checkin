(function () {
    const titleMap = {
        success: '操作成功',
        danger: '出现错误',
        error: '出现错误',
        warning: '需要注意',
        info: '提示',
        primary: '提示',
        secondary: '提示'
    };

    const iconMap = {
        success: '✓',
        danger: '!',
        error: '!',
        warning: '!',
        info: 'i',
        primary: 'i',
        secondary: 'i'
    };

    function normalizeType(type) {
        return Object.prototype.hasOwnProperty.call(titleMap, type) ? type : 'info';
    }

    function getToastStack() {
        let stack = document.getElementById('toast-stack');

        if (!stack) {
            stack = document.createElement('div');
            stack.id = 'toast-stack';
            stack.className = 'toast-stack';
            stack.setAttribute('aria-live', 'polite');
            stack.setAttribute('aria-atomic', 'true');
            document.body.appendChild(stack);
        }

        return stack;
    }

    function resolveMessageArea(areaId) {
        if (!areaId || areaId === 'global-message-area') {
            return null;
        }

        let area = document.getElementById(areaId);

        if (!area) {
            area = document.querySelector(areaId);
        }

        if (!area && areaId.includes(' ')) {
            const firstSpace = areaId.indexOf(' ');
            const idPart = areaId.slice(0, firstSpace);
            const rest = areaId.slice(firstSpace);
            const escapedId = window.CSS && window.CSS.escape
                ? window.CSS.escape(idPart)
                : idPart.replace(/([ !"#$%&'()*+,./:;<=>?@[\\\]^`{|}~])/g, '\\$1');
            area = document.querySelector(`#${escapedId}${rest}`);
        }

        return area;
    }

    function showInlineAlert(message, type, area) {
        const alertDiv = document.createElement('div');
        alertDiv.className = `alert alert-${type} alert-dismissible fade show`;
        alertDiv.setAttribute('role', 'alert');

        const messageDiv = document.createElement('div');
        messageDiv.textContent = message;

        const closeButton = document.createElement('button');
        closeButton.type = 'button';
        closeButton.className = 'close';
        closeButton.setAttribute('data-dismiss', 'alert');
        closeButton.setAttribute('aria-label', 'Close');
        closeButton.innerHTML = '<span aria-hidden="true">&times;</span>';

        alertDiv.appendChild(messageDiv);
        alertDiv.appendChild(closeButton);
        area.appendChild(alertDiv);

        setTimeout(() => {
            if (window.jQuery && window.jQuery.fn.alert) {
                window.jQuery(alertDiv).alert('close');
            } else {
                alertDiv.remove();
            }
        }, 5000);
    }

    function removeToast(toast) {
        toast.classList.remove('is-visible');
        toast.classList.add('is-hiding');
        window.setTimeout(() => toast.remove(), 180);
    }

    window.showAlert = function (message, type = 'info', areaId = 'global-message-area', options = {}) {
        const normalizedType = normalizeType(type);
        const area = resolveMessageArea(areaId);

        if (area) {
            showInlineAlert(message, normalizedType, area);
            return;
        }

        const stack = getToastStack();
        const toast = document.createElement('div');
        const duration = Number.isFinite(options.duration) ? options.duration : 5000;

        toast.className = `app-toast app-toast--${normalizedType}`;
        toast.setAttribute('role', normalizedType === 'danger' || normalizedType === 'error' ? 'alert' : 'status');

        const icon = document.createElement('div');
        icon.className = 'app-toast__icon';
        icon.textContent = iconMap[normalizedType];

        const content = document.createElement('div');
        content.className = 'app-toast__content';

        const title = document.createElement('div');
        title.className = 'app-toast__title';
        title.textContent = options.title || titleMap[normalizedType];

        const messageDiv = document.createElement('div');
        messageDiv.className = 'app-toast__message';
        messageDiv.textContent = message;

        const closeButton = document.createElement('button');
        closeButton.type = 'button';
        closeButton.className = 'app-toast__close';
        closeButton.setAttribute('aria-label', '关闭提示');
        closeButton.innerHTML = '&times;';

        content.appendChild(title);
        content.appendChild(messageDiv);
        toast.appendChild(icon);
        toast.appendChild(content);
        toast.appendChild(closeButton);
        stack.appendChild(toast);

        window.requestAnimationFrame(() => {
            toast.classList.add('is-visible');
        });

        const timer = window.setTimeout(() => removeToast(toast), duration);

        closeButton.addEventListener('click', () => {
            window.clearTimeout(timer);
            removeToast(toast);
        });
    };
})();
