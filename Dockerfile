FROM python:3.11-slim

WORKDIR /app

# نسخ الملفات أولاً
COPY pyproject.toml .
COPY free_llm/ free_llm/
COPY providers.toml .

# تثبيت البرنامج
RUN pip install --no-cache-dir -e .

# المنفذ الافتراضي
EXPOSE 8787

# الأمر الافتراضي: الخادم
CMD ["free-llm", "serve", "--host", "0.0.0.0"]
