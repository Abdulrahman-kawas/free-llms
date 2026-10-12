FROM python:3.11-slim

WORKDIR /app

# تثبيت البرنامج
COPY pyproject.toml .
RUN pip install --no-cache-dir -e .

# نسخ الملفات
COPY free_llm/ free_llm/
COPY providers.toml .

# حالة الخادم
VOLUME /app/.state.json

# المنفذ الافتراضي
EXPOSE 8787

# الأمر الافتراضي: الخادم
CMD ["free-llm", "serve", "--host", "0.0.0.0"]
