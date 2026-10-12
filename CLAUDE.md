# CLAUDE.md — free-llm Microservices Architecture

**free-llm** هي بيئة موحدة للنماذج المجانية (Gemini, Groq, Z AI, Ollama Cloud, Cloudflare) مع تبديل تلقائي والقدرة على الاستراحة. ركيزة المشروع: **عزل البيانات التام بين الخدمات**.

---

## البنية المعمارية (Microservices)

### المستويات الأربعة

```
┌─────────────────────────────────────────────────────────┐
│ Layer 1: التطبيق (CLI + Serve)                          │
│ ├─ cli.py: أوامر مباشرة (list, ask, ping, check)      │
│ ├─ server.py: endpoint OpenAI-compatible (localhost)   │
│ └─ __main__.py: نقطة الدخول                            │
└─────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────┐
│ Layer 2: التوجيه والحالة (Router)                      │
│ ├─ router.py: منطق التبديل بين الموديلات               │
│ ├─ قراءة/كتابة .state.json (حالة خاصة بـ router)      │
│ └─ منطق retry والاستراحة                              │
└─────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────┐
│ Layer 3: المزودون (Provider Isolation)                │
│ ├─ Gemini (Google)        ← مفتاح Google               │
│ ├─ Groq                   ← مفتاح Groq                 │
│ ├─ Z AI                   ← مفتاح Z AI                 │
│ ├─ Ollama Cloud           ← مفتاح Ollama              │
│ └─ Cloudflare             ← مفتاح Cloudflare          │
│                                                         │
│ **العزل الكامل:**                                      │
│ • كل مزود له credentials منفصلة (بـ .env)            │
│ • لا يعرف أحدهم عن الآخر                              │
│ • الخطأ في مزود ≠ خطأ في الآخرين                      │
└─────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────┐
│ Layer 4: الموارد الخارجية (External APIs)             │
│ ├─ Gemini API (Google Cloud)                           │
│ ├─ Groq API                                            │
│ ├─ Z AI API                                            │
│ ├─ Ollama Cloud API                                    │
│ └─ Cloudflare API                                      │
└─────────────────────────────────────────────────────────┘
```

---

## عزل البيانات (Data Isolation)

### 1. منطق عزل المتغيرات (Environment)

```
.env                           # المفاتيح السرية (بـ .gitignore)
├─ GEMINI_API_KEY            # منفصل تماماً
├─ GROQ_API_KEY              # منفصل تماماً
├─ ZAI_API_KEY               # منفصل تماماً
├─ OLLAMA_API_KEY            # منفصل تماماً
├─ CLOUDFLARE_API_KEY        # منفصل تماماً
└─ FREE_LLM_PROXY_KEY        # (اختياري) للخادم فقط

providers.toml                 # التكوين العام (بلا مفاتيح)
├─ ترتيب المحاولة (order)
├─ الحدود اليومية (daily_limit)
└─ الموديلات المتاحة

.state.json                    # الحالة الديناميكية (بـ .gitignore)
└─ معلومات الاستراحة والكوتا فقط
```

### 2. الحدود بين الخدمات

| الطبقة | المسؤول | البيانات المسموحة | البيانات الممنوعة |
|-------|--------|-------------------|-------------------|
| CLI | المستخدم | أوامر فقط | الإنجاز التفاصيلي |
| Router | المنطق | حالة المحاولة، الكوتا | credentials |
| Provider | المزود | طلب واحد منعزل | معلومات عن مزودين آخرين |

**القاعدة الذهبية:** لا يسمح لأي طبقة أن تعرف تفاصيل طبقة أخرى.

---

## تجنب تسرب الميزات

### المشكلة ✗

```python
# خطأ: المزودون يعرف كل واحد عن الثاني
class Gemini:
    def should_skip(self):
        if groq_is_broken():  # ← يعرف عن Groq!
            return True
        return False
```

### الحل ✓

```python
# صحيح: كل مزود مستقل تماماً
class Gemini:
    def should_skip(self):
        # فحص حالتي فقط
        return self.is_in_cooldown() or self.quota_exhausted()

# Router يقرر التبديل
class Router:
    def next_provider(self):
        for provider in self.providers:
            if not provider.should_skip():
                return provider
        raise AllProvidersExhausted()
```

### الأمثلة الأخرى

```python
# ✗ خطأ: state.json يحتوي على بيانات مزود معين
{
    "last_used": "gemini",
    "gemini_daily_used": 15,
    "groq_error_count": 3
}

# ✓ صحيح: state.json عام فقط
{
    "last_used": "gemini",
    "providers_rest_until": {
        "gemini": "2026-10-13T00:30:00Z",
        "groq": "2026-10-13T00:45:00Z"
    }
}

# ✗ خطأ: router.py يحتوي على منطق Groq
def handle_groq_429():
    if groq_special_error_code == 19238:
        # ← منطق خاص بـ Groq هون!
        pass

# ✓ صحيح: groq provider يعالج أخطاءه
class GroqProvider:
    def handle_error(self, response):
        if response.status == 429:
            if "weekly_limit" in response.text:
                self.rest_until = tomorrow_midnight()
```

---

## حدود البيانات (Module Boundaries)

### ما يمكن لـ router.py أن يفعله
- ✓ قراءة `providers.toml`
- ✓ قراءة/كتابة `.state.json` (الحالة العامة فقط)
- ✓ استدعاء كل مزود بتتابع
- ✓ اختيار أول مزود لا يقول "أنا مستراح"

### ما لا يمكن
- ✗ قراءة credentials (بـ router)
- ✗ معرفة تفاصيل خطأ مزود
- ✗ كتابة أخطاء مزود معين لـ state.json

---

## الملفات والمسؤوليات

| الملف | مسؤول عن |
|------|---------|
| `config.py` | تحميل `providers.toml` و `.env` (العزل هنا!) |
| `router.py` | التبديل فقط، **لا يعرف تفاصيل المزودين** |
| `server.py` | endpoint OpenAI-compatible |
| `cli.py` | واجهة المستخدم |
| `.state.json` | حالة مشتركة (وقت الاستراحة فقط) |

---

## أمثلة على العزل

### مثال 1: خطأ 401 (مفتاح خاطئ)

```python
# groq.py (منفصل)
def call(self, prompt):
    response = requests.post(
        "https://api.groq.com/...",
        headers={"Authorization": f"Bearer {self.api_key}"}
    )
    if response.status == 401:
        raise ProviderAuthError("Invalid key")  # ← خطأ منفصل

# router.py (لا يعرف التفاصيل)
def next_provider(self):
    try:
        return selected_provider.call(prompt)
    except ProviderAuthError:
        # router لا يعرف إيش الخطأ، فقط يتجاوز
        return self.next_provider()
```

### مثال 2: حد الكوتا اليومي

```python
# gemini.py
def should_skip(self):
    return self.requests_today >= 20  # عرف نفسك فقط

# router.py
def find_next(self):
    for provider in order:
        if not provider.should_skip():  # يسأل بس "هل أنت جاهز؟"
            return provider
```

---

## نقاط فحص (Checklist)

- [ ] هل كل provider له ملف منفصل؟
- [ ] هل `router.py` لا يحتوي على منطق خاص بمزود معين؟
- [ ] هل credentials قراءتها بـ `config.py` فقط؟
- [ ] هل `.state.json` يحتوي على حالة عامة فقط (بلا أسرار)؟
- [ ] هل كل provider يعيد `ProviderError` (بلا تفاصيل)؟
- [ ] هل الأوامر (CLI) لا تعرف عن الداخل؟

---

## الأوامر الأساسية

```bash
# بناء بيئة التطوير
uv sync

# اختبار بلا انترنت
uvx --with-editable . pytest -q tests

# تثبيت محلي للاختبار
uv tool install -e .

# قائمة المزودين
free-llm list

# اختبار المفاتيح
free-llm check

# سؤال مع تبديل تلقائي
free-llm ask "سؤالك"

# سؤال مزود معين
free-llm ask "سؤالك" -m groq:openai/gpt-oss-120b

# خادم محلي
free-llm serve
```

---

## الملاحظات الأمنية

- المفاتيح الحساسة بـ `.env` فقط
- لا تطبع credentials في السجلات
- كل مزود يتعامل مع أخطاءه بنفسه
- router لا يعرف عن الأخطاء الداخلية
