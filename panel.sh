#!/usr/bin/env bash
# ============================================================
#  لوحة تحكم بوت متجر شرائح eSIM - قائمة طرفية عبر SSH
#  Telegram eSIM Store Bot - VPS Terminal Control Panel
# ============================================================
set -e

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

SERVICE="esim-bot"
BACKUP_ROOT="$PROJECT_DIR/backups"
DB_FILE="$PROJECT_DIR/esim_store.db"

# قراءة مسار قاعدة البيانات من .env إن وُجد
if [[ -f "$PROJECT_DIR/.env" ]]; then
    ENV_DB="$(grep -E '^DB_PATH=' "$PROJECT_DIR/.env" | cut -d= -f2- | tr -d '"' || true)"
    [[ -n "$ENV_DB" ]] && DB_FILE="$ENV_DB"
fi

pause() {
    echo ""
    read -rp "اضغط Enter للعودة إلى القائمة..." _
}

# نسخة احتياطية متسقة لقاعدة بيانات SQLite (باستخدام .backup وليس النسخ المباشر)
# بالإضافة إلى ملف الإعدادات وصور QR. لا تُوقف هذه العملية البوت.
make_backup() {
    local stamp dest
    stamp="$(date +%Y%m%d-%H%M%S)"
    dest="$BACKUP_ROOT/$stamp"
    mkdir -p "$dest"
    [[ -f "$PROJECT_DIR/.env" ]] && cp -a "$PROJECT_DIR/.env" "$dest/.env"
    if [[ -f "$DB_FILE" ]]; then
        if command -v sqlite3 >/dev/null 2>&1; then
            sqlite3 "$DB_FILE" ".backup '$dest/esim_store.db'"
        else
            cp -a "$DB_FILE" "$dest/esim_store.db"
            echo "   ⚠️ sqlite3 غير مثبت؛ تم نسخ قاعدة البيانات نسخاً مباشراً (يفضل التحديث والبوت متوقف)."
        fi
    fi
    [[ -d "$PROJECT_DIR/qr_codes" ]] && cp -a "$PROJECT_DIR/qr_codes" "$dest/qr_codes"
    echo "$dest"
}

service_exists() {
    systemctl cat "$SERVICE.service" >/dev/null 2>&1
}

show_header() {
    clear
    echo "============================================================"
    echo "   🛠  لوحة تحكم بوت متجر شرائح eSIM (VPS Control Panel)"
    echo "============================================================"
    if service_exists && systemctl is-active --quiet "$SERVICE"; then
        echo "   حالة البوت: ✅ يعمل"
    elif service_exists; then
        echo "   حالة البوت: ⛔ متوقف"
    else
        echo "   حالة البوت: ⚠️ خدمة $SERVICE غير مثبتة (شغّل install.sh أولاً)"
    fi
    echo "   مسار المشروع: $PROJECT_DIR"
    echo "============================================================"
    echo ""
}

do_update() {
    echo "🔄 التحديث إلى أحدث إصدار من GitHub (مع الحفاظ على قاعدة البيانات والإعدادات)"
    echo "------------------------------------------------------------"

    if ! command -v git >/dev/null 2>&1; then
        echo "❌ git غير مثبت على الخادم. ثبّته أولاً: sudo apt-get install -y git"
        pause; return
    fi
    if ! git rev-parse --git-dir >/dev/null 2>&1; then
        echo "❌ هذا المجلد ليس نسخة Git. أعد التثبيت عبر git clone ثم install.sh"
        pause; return
    fi

    # 1) حماية التعديلات المحلية: نرفض التحديث إذا كانت هناك ملفات متتبعة معدلة
    if ! git diff --quiet || ! git diff --cached --quiet; then
        echo "❌ توجد تعديلات محلية على ملفات المشروع المتتبعة في Git:"
        git status --short
        echo ""
        echo "لم يتم تغيير أي شيء. احفظ تعديلاتك أولاً (مثلاً: git stash أو نسخة احتياطية)"
        echo "ثم أعد المحاولة. لن يقوم هذا الخيار بحذف أو الكتابة فوق تعديلاتك."
        pause; return
    fi

    echo "1) جلب معلومات أحدث إصدار من GitHub..."
    git fetch origin
    # تحديد الفرع الرئيسي بأكثر من طريقة (GitHub عادة main)
    BRANCH="$(git remote show origin 2>/dev/null | sed -n 's/^.*HEAD branch: //p' | head -1)"
    if [[ -z "$BRANCH" || "$BRANCH" == *"("* ]]; then
        for cand in main master; do
            if git rev-parse --verify --quiet "origin/$cand" >/dev/null; then
                BRANCH="$cand"
                break
            fi
        done
    fi
    if [[ -z "$BRANCH" ]] || ! git rev-parse --verify --quiet "origin/$BRANCH" >/dev/null; then
        echo "❌ تعذر تحديد الفرع الرئيسي على GitHub. لم يتغير أي شيء."
        pause; return
    fi

    if git rev-parse HEAD >/dev/null 2>&1 && [[ "$(git rev-parse HEAD)" == "$(git rev-parse "origin/$BRANCH")" ]]; then
        echo "✅ البوت محدث بالفعل إلى أحدث إصدار. لا حاجة لأي تغيير."
        pause; return
    fi

    # 2) فحص تعارض المسارات: نتأكد أن قاعدة البيانات/.env/qr_codes غير متتبعة في الإصدار الجديد
    echo "2) فحص حماية قاعدة البيانات والإعدادات في الإصدار الجديد..."
    if git ls-tree -r --name-only "origin/$BRANCH" | grep -qE '(^|/)(\.env|[^/]*\.db)$|(^|/)qr_codes/'; then
        echo "❌ تحذير: الإصدار الجديد على GitHub يتضمن ملفات بيانات (قاعدة بيانات أو .env أو qr_codes)."
        echo "المتابعة قد تستبدل بياناتك المحلية. تم إيقاف التحديث حمايةً لبياناتك؛ راجع المستودع أولاً."
        pause; return
    fi

    # 3) التحقق من أن الإصدار الجديد يتضمن ملفات الميزات الحالية حتى لا تُفقد عند التحديث
    echo "3) التحقق من وجود ملفات الميزات في الإصدار الجديد..."
    MISSING=""
    for f in bot.py database.py config.py panel.sh; do
        if ! git cat-file -e "origin/$BRANCH:$f" 2>/dev/null; then
            MISSING="$MISSING $f"
        fi
    done
    if [[ -n "$MISSING" ]]; then
        echo "⚠️ تنبيه مهم: الإصدار الموجود على GitHub لا يتضمن الملفات التالية:$MISSING"
        echo "التحديث الآن سيحذف هذه الميزات من نسختك المحلية (مثل لوحة التحكم أو نظام الاسترداد)."
        echo "الأفضل رفع نسختك الحالية إلى GitHub أولاً ثم إعادة التحديث."
        read -rp "هل تريد المتابعة رغم ذلك؟ اكتب (نعم) للمتابعة أو أي شيء آخر للإلغاء: " CONFIRM
        if [[ "$CONFIRM" != "نعم" ]]; then
            echo "تم إلغاء التحديث. لم يتغير أي شيء."
            pause; return
        fi
    fi

    # 4) نسخة احتياطية متسقة قبل أي تغيير
    echo "4) إنشاء نسخة احتياطية متسقة (قاعدة البيانات + الإعدادات + صور QR)..."
    BK="$(make_backup)"
    echo "   ✅ النسخة الاحتياطية: $BK"

    OLD_SHA="$(git rev-parse HEAD)"

    # 5) التحديث بطريقة آمنة (fast-forward فقط، بدون حذف للتعديلات المحلية)
    echo "5) سحب الإصدار الجديد..."
    if ! git merge --ff-only "origin/$BRANCH"; then
        echo "❌ تعذر التحديث التلقائي (تفرع في سجل الإصدارات). لم يتغير أي شيء."
        echo "   راجع المستودع يدوياً أو تواصل مع المطور."
        pause; return
    fi

    rollback() {
        echo "↩️ التراجع عن التحديث واستعادة الإصدار السابق..."
        git reset --hard "$OLD_SHA" || true
        if service_exists; then sudo systemctl restart "$SERVICE" || true; fi
        echo "تمت استعادة الإصدار السابق. النسخة الاحتياطية محفوظة في: $BK"
    }

    # 6) تحديث المكتبات
    echo "6) تحديث المكتبات البرمجية..."
    if [[ -x "$PROJECT_DIR/venv/bin/pip" ]]; then
        if ! "$PROJECT_DIR/venv/bin/pip" install -r "$PROJECT_DIR/requirements.txt"; then
            echo "❌ فشل تثبيت المكتبات."
            rollback
            pause; return
        fi
    else
        echo "   ⚠️ بيئة venv غير موجودة، أعد تشغيل install.sh"
        rollback
        pause; return
    fi

    # 7) ترحيل قاعدة البيانات (إضافي فقط: ينشئ الجداول الجديدة دون تعديل البيانات)
    echo "7) تحديث بنية قاعدة البيانات (إضافة جداول جديدة فقط إن وُجدت)..."
    if ! (cd "$PROJECT_DIR" && ./venv/bin/python -c "import database; database.init_db()"); then
        echo "❌ فشل تحديث بنية قاعدة البيانات."
        rollback
        pause; return
    fi

    # 8) إعادة التشغيل والتحقق
    echo "8) إعادة تشغيل البوت بالإصدار الجديد..."
    if service_exists; then
        sudo systemctl restart "$SERVICE"
        sleep 2
        if systemctl is-active --quiet "$SERVICE"; then
            echo ""
            echo "✅ تم التحديث بنجاح! البوت يعمل الآن بأحدث إصدار."
            echo "   قاعدة البيانات وملف الإعدادات لم يتغيرا، والنسخة الاحتياطية في: $BK"
        else
            echo "❌ فشل تشغيل البوت بعد التحديث."
            rollback
        fi
    else
        echo "⚠️ خدمة $SERVICE غير مثبتة؛ تم تحديث الملفات فقط. شغّل install.sh لإعداد الخدمة."
        echo "   النسخة الاحتياطية في: $BK"
    fi
    pause
}

while true; do
    show_header
    echo "  1) حالة البوت التفصيلية"
    echo "  2) متابعة سجلات التشغيل المباشرة (Ctrl+C للخروج)"
    echo "  3) إعادة تشغيل البوت"
    echo "  4) إيقاف البوت"
    echo "  5) تشغيل البوت"
    echo "  6) 🔄 التحديث إلى أحدث إصدار (بدون فقدان قاعدة البيانات)"
    echo "  7) إنشاء نسخة احتياطية يدوية (قاعدة البيانات + الإعدادات + QR)"
    echo "  8) تعديل ملف الإعدادات (.env)"
    echo "  0) خروج"
    echo ""
    read -rp "اختر رقماً: " choice

    case "$choice" in
        1)
            echo ""
            if service_exists; then
                sudo systemctl status "$SERVICE" --no-pager || true
            else
                echo "⚠️ خدمة $SERVICE غير مثبتة. شغّل install.sh أولاً."
            fi
            pause
            ;;
        2)
            echo ""
            if service_exists; then
                echo "📜 سجلات البوت المباشرة (اضغط Ctrl+C للعودة للقائمة):"
                sudo journalctl -u "$SERVICE" -f || true
            else
                echo "⚠️ خدمة $SERVICE غير مثبتة. شغّل install.sh أولاً."
                pause
            fi
            ;;
        3)
            if service_exists; then sudo systemctl restart "$SERVICE"; echo "✅ تمت إعادة تشغيل البوت."; else echo "⚠️ الخدمة غير مثبتة."; fi
            pause
            ;;
        4)
            if service_exists; then sudo systemctl stop "$SERVICE"; echo "⛔ تم إيقاف البوت."; else echo "⚠️ الخدمة غير مثبتة."; fi
            pause
            ;;
        5)
            if service_exists; then sudo systemctl start "$SERVICE"; echo "✅ تم تشغيل البوت."; else echo "⚠️ الخدمة غير مثبتة."; fi
            pause
            ;;
        6)
            do_update
            ;;
        7)
            BK="$(make_backup)"
            echo "✅ تم إنشاء نسخة احتياطية في: $BK"
            pause
            ;;
        8)
            "${EDITOR:-nano}" "$PROJECT_DIR/.env"
            echo "💡 أعد تشغيل البوت (الخيار 3) لتطبيق التعديلات."
            pause
            ;;
        0)
            echo "إلى اللقاء! 👋"
            exit 0
            ;;
        *)
            echo "⚠️ اختيار غير صحيح."
            sleep 1
            ;;
    esac
done
