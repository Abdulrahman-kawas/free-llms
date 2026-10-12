# تشغيل free-llm على Docker محلياً

## 1️⃣ التحضير

### المفاتيح (.env)
```bash
# انسخ المثال
cp .env.example .env

# أضف مفاتيحك (الموجودة منها فقط)
GEMINI_API_KEY=sk-...
GROQ_API_KEY=gsk-...
# الخ
```

### providers.toml
تأكد إنه موجود وصحيح:
```bash
ls -l providers.toml
```

---

## 2️⃣ التشغيل المحلي

### أ) البناء والتشغيل الأول
```bash
# بناء الصورة
docker-compose build

# تشغيل الخادم
docker-compose up -d

# شوف السجلات
docker-compose logs -f free-llm
```

### ب) اختبار الخادم
```bash
# هل هو حي؟
curl http://localhost:8787/health

# قائمة الموديلات
curl http://localhost:8787/v1/models

# سؤال تجريبي
curl http://localhost:8787/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "auto",
    "messages": [{"role": "user", "content": "Hi"}]
  }' | jq .
```

### ج) استخدام من Python
```python
import requests

response = requests.post(
    "http://localhost:8787/v1/chat/completions",
    json={
        "model": "auto",
        "messages": [{"role": "user", "content": "Hello"}],
        "max_tokens": 100
    }
)
print(response.json())
```

### د) استخدام OpenAI SDK
```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8787/v1", api_key="any")
reply = client.chat.completions.create(
    model="auto",
    messages=[{"role": "user", "content": "Hi"}]
)
print(reply.choices[0].message.content)
```

---

## 3️⃣ الاختبارات

### داخل الحاوية
```bash
# دخول bash للحاوية
docker-compose exec free-llm bash

# تشغيل الأوامر
free-llm list
free-llm check
free-llm ask "سؤالك"
```

### من الخارج (عبر API)
```bash
# Gemini (مثال)
curl http://localhost:8787/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "gemini:gemini-1.5-flash", "messages": [{"role": "user", "content": "test"}]}' | jq .x_free_llm

# النتيجة:
# {
#   "model": "gemini:gemini-1.5-flash",
#   "skipped": []
# }
```

---

## 4️⃣ الحالة والكوتا

### قراءة الحالة
```bash
# من الخارج (انسخ من الحاوية)
docker-compose exec free-llm cat .state.json | jq .

# أو اطبع المباشرة
curl http://localhost:8787/v1/models | jq '.data[] | select(.ready == false) | {id: .id, why: .why}'
```

### إعادة تعيين الاستراحات
```bash
docker-compose exec free-llm free-llm reset
docker-compose exec free-llm free-llm reset gemini
```

---

## 5️⃣ التطوير

### تعديل الكود والاختبار
```bash
# أوقف الخادم
docker-compose down

# عدّل الملفات
vim free_llm/router.py

# أعد بناء وشغّل
docker-compose up --build
```

### نسخ الحالة للاختبار التالي
```bash
# احفظ state.json
docker-compose exec free-llm cat .state.json > state-backup.json

# أعد التشغيل
docker-compose down
docker-compose up -d

# استعد الحالة
cat state-backup.json | docker-compose exec -T free-llm tee .state.json
```

---

## 6️⃣ الفحوصات قبل Amazon

```bash
#!/bin/bash

echo "🔍 Checking..."

# 1. الصحة
curl -f http://localhost:8787/health || exit 1

# 2. قائمة الموديلات
curl -f http://localhost:8787/v1/models | jq . || exit 1

# 3. عدد الموديلات الجاهزة
READY=$(curl -s http://localhost:8787/v1/models | jq '[.data[] | select(.ready == true)] | length')
echo "✅ Ready models: $READY"
[ "$READY" -gt 0 ] || exit 1

# 4. سؤال تجريبي (Gemini مثلاً)
curl -f http://localhost:8787/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "gemini:gemini-1.5-flash", "messages": [{"role": "user", "content": "hi"}]}' \
  | jq '.choices[0].message.content' || exit 1

echo "✅ All checks passed! Ready for AWS."
```

---

## 7️⃣ النشر على Amazon

### الصورة الموجودة محلياً
```bash
# إنشاء الصورة
docker build -t free-llm:latest .

# اختبار الصورة
docker run -p 8787:8787 \
  -e GEMINI_API_KEY=$GEMINI_API_KEY \
  -e GROQ_API_KEY=$GROQ_API_KEY \
  free-llm:latest
```

### رفع إلى AWS ECR
```bash
# أنشئ repo (مرة واحدة)
aws ecr create-repository --repository-name free-llm --region us-east-1

# أنشئ tag
docker tag free-llm:latest $AWS_ACCOUNT.dkr.ecr.us-east-1.amazonaws.com/free-llm:latest

# ادفع الصورة
docker push $AWS_ACCOUNT.dkr.ecr.us-east-1.amazonaws.com/free-llm:latest
```

### الخادم على ECS/Fargate
```bash
# نموذج task definition (JSON)
{
  "family": "free-llm",
  "networkMode": "awsvpc",
  "requiresCompatibilities": ["FARGATE"],
  "cpu": "256",
  "memory": "512",
  "containerDefinitions": [{
    "name": "free-llm",
    "image": "$AWS_ACCOUNT.dkr.ecr.us-east-1.amazonaws.com/free-llm:latest",
    "portMappings": [{"containerPort": 8787}],
    "environment": [
      {"name": "FREE_LLM_PROXY_KEY", "value": "your-secret-key"}
    ],
    "secrets": [
      {"name": "GEMINI_API_KEY", "valueFrom": "arn:aws:secretsmanager:..."},
      {"name": "GROQ_API_KEY", "valueFrom": "arn:aws:secretsmanager:..."}
    ],
    "healthCheck": {
      "command": ["CMD-SHELL", "curl -f http://localhost:8787/health || exit 1"],
      "interval": 30,
      "timeout": 5,
      "retries": 3
    }
  }]
}
```

---

## 8️⃣ الإيقاف والتنظيف

```bash
# إيقاف الخادم
docker-compose down

# حذف الحاوية والصورة
docker-compose down -v --rmi all

# نظّف الـ volumes
docker volume rm free-llms_free-llm-state
```

---

## الأوامر السريعة

```bash
docker-compose build                    # بناء
docker-compose up -d                    # تشغيل
docker-compose down                     # إيقاف
docker-compose logs -f                  # سجلات
docker-compose exec free-llm bash       # دخول bash
docker-compose ps                       # حالة الخدمات
```
