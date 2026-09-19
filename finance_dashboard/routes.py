from pathlib import Path
from urllib.parse import urlsplit

from flask import Blueprint, Response, flash, g, redirect, render_template, request, send_file, url_for

from .auth import authenticate, change_password, login_required, login_user, logout_user, require_csrf
from .importers import ImportValidationError
from .services import (
    build_template_csv,
    create_investment_plan_target,
    create_backup,
    create_cash_note_movement,
    create_manual_adjustment,
    create_recurring_expense,
    delete_manual_adjustment,
    delete_recurring_expense,
    export_summary_csv,
    get_bank_context,
    get_bank_subbalances_context,
    get_budget_context,
    get_cash_context,
    get_dashboard_context,
    get_fixed_expenses_context,
    get_history_context,
    get_import_context,
    get_investment_plan_context,
    get_investments_context,
    get_portfolio_context,
    get_savings_context,
    get_settings_context,
    import_uploaded_file,
    recount_cash_inventory,
    refresh_crypto_market_prices,
    toggle_recurring_expense,
    toggle_investment_plan_target,
    update_default_budget_rule,
    update_investment_plan_target,
    update_reporting_baseline,
    update_transaction_category,
    upsert_monthly_budget,
    upsert_salary_entry,
)


web = Blueprint("web", __name__)


def _current_month_arg() -> str:
    return request.args.get("month", "").strip() or request.form.get("month", "").strip()


def _safe_next_url(candidate: str | None, fallback: str) -> str:
    if not candidate:
        return fallback
    parsed = urlsplit(candidate)
    if (
        parsed.scheme
        or parsed.netloc
        or "\\" in candidate
        or not parsed.path.startswith("/")
        or parsed.path.startswith("//")
    ):
        return fallback
    return candidate


@web.route("/")
def home():
    if g.get("user"):
        return redirect(url_for("web.dashboard"))
    return redirect(url_for("web.login"))


@web.route("/login", methods=["GET", "POST"])
def login():
    if g.get("user"):
        return redirect(url_for("web.dashboard"))

    if request.method == "POST":
        require_csrf()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = authenticate(username, password)
        if not user:
            flash("Usuario o contrasena incorrectos.", "error")
        else:
            login_user(user)
            next_url = _safe_next_url(request.args.get("next"), url_for("web.dashboard"))
            flash("Sesion iniciada.", "success")
            return redirect(next_url)

    return render_template("login.html", page_title="Iniciar sesion")


@web.post("/logout")
@login_required
def logout():
    require_csrf()
    logout_user()
    flash("Sesion cerrada.", "success")
    return redirect(url_for("web.login"))


@web.get("/resumen")
@login_required
def dashboard():
    return render_template("dashboard.html", **get_dashboard_context(request.args.get("month")))


@web.route("/presupuesto", methods=["GET", "POST"])
@login_required
def budget():
    selected_month = _current_month_arg()
    if request.method == "POST":
        require_csrf()
        action = request.form.get("action", "").strip()
        try:
            if action == "save_budget":
                upsert_monthly_budget(selected_month, request.form)
                flash("Presupuesto mensual guardado.", "success")
            elif action == "save_salary":
                upsert_salary_entry(selected_month, request.form)
                flash("Nomina mensual guardada.", "success")
            elif action == "add_adjustment":
                create_manual_adjustment(selected_month, request.form)
                flash("Ajuste manual anadido.", "success")
            else:
                flash("Accion de presupuesto no reconocida.", "error")
        except ValueError as exc:
            flash(str(exc), "error")
        return redirect(url_for("web.budget", month=selected_month))

    return render_template("budget.html", **get_budget_context(request.args.get("month")))


@web.post("/presupuesto/ajustes/<int:adjustment_id>/eliminar")
@login_required
def budget_delete_adjustment(adjustment_id: int):
    require_csrf()
    month = request.form.get("month", "").strip()
    delete_manual_adjustment(adjustment_id)
    flash("Ajuste eliminado.", "success")
    return redirect(url_for("web.budget", month=month or None))


@web.get("/banco")
@login_required
def bank():
    return render_template(
        "bank.html",
        **get_bank_context(
            selected_month=request.args.get("month"),
            category_code=request.args.get("category"),
            review_status=request.args.get("review"),
            search=request.args.get("search"),
        ),
    )


@web.get("/banco/bolsas")
@login_required
def bank_subbalances():
    return render_template("bank_subbalances.html", **get_bank_subbalances_context(request.args.get("month")))


@web.get("/efectivo")
@login_required
def cash():
    return render_template("cash.html", **get_cash_context())


@web.post("/efectivo/movimiento")
@login_required
def cash_movement():
    require_csrf()
    try:
        result = create_cash_note_movement(request.form)
        action_label = "anadido" if result["direction"] == "in" else "retirado"
        flash(
            f"Has {action_label} {result['quantity']} billetes de {result['denomination']} EUR ({result['amount']:.2f} EUR).",
            "success",
        )
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("web.cash"))


@web.post("/efectivo/recontar")
@login_required
def cash_recount():
    require_csrf()
    try:
        result = recount_cash_inventory(request.form)
        if abs(result["delta"]) > 0:
            flash(
                (
                    f"Recuento guardado. El efectivo pasa de {result['old_total']:.2f} EUR "
                    f"a {result['new_total']:.2f} EUR."
                ),
                "success",
            )
        else:
            flash("Recuento guardado. El total de efectivo no cambia.", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("web.cash"))


@web.post("/banco/movimiento/<int:transaction_id>/categoria")
@login_required
def update_bank_transaction_category(transaction_id: int):
    require_csrf()
    month = request.form.get("month", "").strip()
    category = request.form.get("category_code", "").strip()
    review_notes = request.form.get("review_notes", "").strip()
    is_fixed_expense = request.form.get("is_fixed_expense") == "1"
    try:
        update_transaction_category(transaction_id, category, review_notes, is_fixed_expense)
        flash("Movimiento actualizado.", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(
        url_for(
            "web.bank",
            month=month or None,
            category=request.form.get("category_filter") or None,
            review=request.form.get("review_filter") or None,
            search=request.form.get("search_filter") or None,
        )
    )


@web.get("/inversiones")
@login_required
def investments():
    return render_template("investments.html", **get_investments_context(request.args.get("month")))


@web.post("/inversiones/refrescar-precios")
@login_required
def refresh_investment_prices():
    require_csrf()
    result = refresh_crypto_market_prices(force=True)
    if result.get("updated"):
        flash(f"Precios live actualizados: {result['updated']} activos.", "success")
    else:
        message = "No se pudieron actualizar precios live en este momento."
        if result.get("error"):
            message += f" {result['error']}"
        flash(message, "warning")
    return redirect(_safe_next_url(request.form.get("next"), url_for("web.investments")))


@web.get("/trade-republic")
@login_required
def trade_republic():
    return render_template("portfolio_source.html", **get_portfolio_context("trade_republic"))


@web.get("/binance")
@login_required
def binance():
    return render_template("portfolio_source.html", **get_portfolio_context("binance"))


@web.get("/ahorro")
@login_required
def savings():
    return render_template("savings.html", **get_savings_context(request.args.get("month")))


@web.route("/gastos-fijos", methods=["GET", "POST"])
@login_required
def fixed_expenses():
    selected_month = _current_month_arg()
    if request.method == "POST":
        require_csrf()
        try:
            create_recurring_expense(request.form)
            flash("Gasto recurrente guardado.", "success")
        except ValueError as exc:
            flash(str(exc), "error")
        return redirect(url_for("web.fixed_expenses", month=selected_month))
    return render_template("fixed_expenses.html", **get_fixed_expenses_context(request.args.get("month")))


@web.post("/gastos-fijos/<int:expense_id>/toggle")
@login_required
def fixed_expenses_toggle(expense_id: int):
    require_csrf()
    toggle_recurring_expense(expense_id)
    flash("Estado del gasto recurrente actualizado.", "success")
    return redirect(url_for("web.fixed_expenses", month=request.form.get("month") or None))


@web.post("/gastos-fijos/<int:expense_id>/eliminar")
@login_required
def fixed_expenses_delete(expense_id: int):
    require_csrf()
    delete_recurring_expense(expense_id)
    flash("Gasto recurrente eliminado.", "success")
    return redirect(url_for("web.fixed_expenses", month=request.form.get("month") or None))


@web.get("/historico")
@login_required
def history():
    return render_template("history.html", **get_history_context(request.args.get("start")))


@web.route("/importar", methods=["GET", "POST"])
@login_required
def import_data():
    if request.method == "POST":
        require_csrf()
        source = request.form.get("source", "").strip()
        snapshot_date = request.form.get("snapshot_date", "").strip() or None
        uploaded_file = request.files.get("import_file")

        try:
            result = import_uploaded_file(
                source=source,
                uploaded_file=uploaded_file,
                snapshot_date=snapshot_date,
                created_by=g.user["username"],
            )
            message = (
                f"Importacion completada: {result['inserted']} filas nuevas, "
                f"{result['duplicates']} duplicadas y {result['classified']} movimientos clasificados."
            )
            if result["pending_review"]:
                message += f" Quedan {result['pending_review']} movimientos pendientes de revisar."
            if result["warnings"]:
                message += " Hay posiciones pendientes de valorar."
            flash(message, "success")
            return redirect(url_for("web.import_data"))
        except ImportValidationError as exc:
            flash(str(exc), "error")
        except Exception as exc:
            flash(f"No se pudo importar el archivo: {exc}", "error")

    return render_template("import.html", **get_import_context())


@web.get("/importar/plantilla/<source>")
@login_required
def download_template(source: str):
    content = build_template_csv(source)
    return Response(
        content,
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{source}_template.csv"'},
    )


@web.get("/configuracion")
@login_required
def settings():
    return render_template("settings.html", **get_settings_context())


@web.route("/configuracion/plan-inversion", methods=["GET", "POST"])
@login_required
def investment_plan():
    selected_month = _current_month_arg()
    if request.method == "POST":
        require_csrf()
        try:
            create_investment_plan_target(request.form)
            flash("Linea del plan de inversion guardada.", "success")
        except ValueError as exc:
            flash(str(exc), "error")
        return redirect(url_for("web.investment_plan", month=selected_month or None))
    return render_template("investment_plan.html", **get_investment_plan_context(request.args.get("month")))


@web.post("/configuracion/plan-inversion/<int:target_id>/actualizar")
@login_required
def investment_plan_update(target_id: int):
    require_csrf()
    month = request.form.get("month", "").strip()
    try:
        update_investment_plan_target(target_id, request.form)
        flash("Objetivo de inversion actualizado.", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("web.investment_plan", month=month or None))


@web.post("/configuracion/plan-inversion/<int:target_id>/toggle")
@login_required
def investment_plan_toggle(target_id: int):
    require_csrf()
    toggle_investment_plan_target(target_id)
    flash("Estado del objetivo de inversion actualizado.", "success")
    return redirect(url_for("web.investment_plan", month=request.form.get("month") or None))


@web.post("/configuracion/presupuesto")
@login_required
def update_default_budget():
    require_csrf()
    update_default_budget_rule(request.form)
    flash("Configuracion base del presupuesto actualizada.", "success")
    return redirect(url_for("web.settings"))


@web.post("/configuracion/baseline")
@login_required
def update_baseline():
    require_csrf()
    next_url = _safe_next_url(request.form.get("next"), url_for("web.settings"))
    try:
        baseline_date = update_reporting_baseline(request.form)
        flash(f"Inicio oficial del historico actualizado a {baseline_date}.", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(next_url)


@web.post("/configuracion/password")
@login_required
def update_password():
    require_csrf()
    current_password = request.form.get("current_password", "")
    new_password = request.form.get("new_password", "")
    success, message = change_password(g.user["id"], current_password, new_password)
    flash(message, "success" if success else "error")
    return redirect(url_for("web.settings"))


@web.post("/configuracion/backup")
@login_required
def backup():
    require_csrf()
    backup_path = create_backup()
    flash(f"Backup creado en {backup_path.name}.", "success")
    return redirect(url_for("web.settings"))


@web.get("/descargas/backup/<filename>")
@login_required
def download_backup(filename: str):
    if Path(filename).name != filename:
        flash("Nombre de backup invalido.", "error")
        return redirect(url_for("web.settings"))

    backups_dir = Path(get_settings_context()["stats"]["backups_dir"])
    target = backups_dir / filename
    if not target.exists():
        flash("El backup solicitado ya no existe.", "error")
        return redirect(url_for("web.settings"))
    return send_file(target, as_attachment=True, download_name=target.name)


@web.get("/export/resumen.csv")
@login_required
def export_summary():
    content = export_summary_csv()
    return Response(
        content,
        mimetype="text/csv",
        headers={"Content-Disposition": 'attachment; filename="resumen_finanzas.csv"'},
    )
