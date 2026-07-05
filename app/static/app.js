function t(key, fallback) {
  if (window.I18N && Object.prototype.hasOwnProperty.call(window.I18N, key)) {
    return window.I18N[key];
  }
  return fallback || key;
}

function statusLabel(status) {
  return t(`status.${status}`, status || t("common.none", "None"));
}

function confirmationStatusLabel(status) {
  return t(`confirmation.${status}`, status || t("common.none", "None"));
}

function realtimeStatusLabel(status) {
  return t(`realtime.${status}`, status || t("realtime.rest_only", "参考データ"));
}

(function () {
  function csrfToken() {
    return document.body.dataset.csrfToken || "";
  }

  function postHeaders(extra) {
    var headers = {"Content-Type": "application/json", "X-CSRF-Token": csrfToken()};
    if (extra) {
      Object.keys(extra).forEach(function (key) {
        if (extra[key]) headers[key] = extra[key];
      });
    }
    return headers;
  }

  function moneylessReturn(stake, probability) {
    if (!probability || probability <= 0) return 0;
    return stake / probability;
  }

  function updateEstimator() {
    var form = document.getElementById("prediction-form");
    if (!form) return;
    var select = document.getElementById("outcome");
    var stakeInput = document.getElementById("stake");
    var probability = Number(select.options[select.selectedIndex].dataset.probability || "0");
    var stake = Number(stakeInput.value || "0");
    document.getElementById("selected-probability").textContent = (probability * 100).toFixed(1) + "%";
    document.getElementById("estimated-return").textContent = moneylessReturn(stake, probability).toFixed(2);
    document.getElementById("max-loss").textContent = stake.toFixed(2);
  }

  function attachPredictionForm() {
    var form = document.getElementById("prediction-form");
    if (!form) return;
    form.addEventListener("change", updateEstimator);
    form.addEventListener("input", updateEstimator);
    form.addEventListener("submit", async function (event) {
      event.preventDefault();
      var result = document.getElementById("prediction-result");
      result.textContent = "";
      var payload = {
        market_id: form.dataset.marketId,
        outcome: document.getElementById("outcome").value,
        stake: document.getElementById("stake").value
      };
      try {
        var response = await fetch("/api/demo/predict", {
          method: "POST",
          headers: postHeaders(),
          body: JSON.stringify(payload)
        });
        var data = await response.json();
        if (!response.ok) {
          result.textContent = data.detail || "デモ参加を記録できませんでした。";
          result.className = "form-message error";
          return;
        }
        var balance = Number(data.balance).toFixed(2);
        result.textContent = (data.message || "デモ参加を記録しました。") + " デモ残高: " + balance;
        result.className = "form-message success";
        var balanceNode = document.getElementById("demo-balance");
        if (balanceNode) balanceNode.textContent = balance;
      } catch (error) {
        result.textContent = "通信に失敗しました。";
        result.className = "form-message error";
      }
    });
    updateEstimator();
  }

  function statusBadge(status) {
    if (status === "live") return "外部参考データ";
    if (status === "sample_fallback") return "参考データ";
    if (status === "live_failed_sample_fallback" || status === "live_empty_sample_fallback") {
      return "参考データ";
    }
    return "参考データ";
  }

  function formatMinute(value) {
    if (!value) return "-";
    var parsed = new Date(value);
    if (!Number.isNaN(parsed.getTime())) {
      var year = parsed.getFullYear();
      var month = String(parsed.getMonth() + 1).padStart(2, "0");
      var day = String(parsed.getDate()).padStart(2, "0");
      var hour = String(parsed.getHours()).padStart(2, "0");
      var minute = String(parsed.getMinutes()).padStart(2, "0");
      return `${year}-${month}-${day} ${hour}:${minute}`;
    }
    return String(value).replace("T", " ").slice(0, 16);
  }

  async function pollMarkets() {
    var list = document.getElementById("market-list");
    if (!list) return;
    try {
      var response = await fetch("/api/markets");
      if (!response.ok) return;
      var data = await response.json();
      if (!data.markets || !data.markets.length) return;
      document.getElementById("data-status").textContent = statusBadge(data.markets[0].data_source_status);
      document.getElementById("freshness").textContent = formatMinute(data.markets[0].fetched_at);
      var displayed = document.getElementById("displayed-count");
      var total = document.getElementById("total-count");
      if (displayed) displayed.textContent = data.count;
      if (total) total.textContent = data.total_market_count;
    } catch (error) {
      var status = document.getElementById("data-status");
      if (status) status.textContent = "更新確認中";
    }
  }

  async function pollRealtimeStatus() {
    var summary = document.getElementById("realtime-summary");
    if (!summary) return;
    try {
      var response = await fetch("/api/realtime/status");
      if (!response.ok) return;
      var data = await response.json();
      if (!data.ws_enabled) {
        summary.textContent = "参考データ";
      } else if (data.live_market_update_count > 0) {
        summary.textContent = "最新情報を自動更新";
      } else if (data.stale_market_update_count > 0) {
        summary.textContent = "更新確認中";
      } else {
        summary.textContent = "参考データ";
      }
    } catch (error) {
      summary.textContent = "参考データ";
    }
  }

  function setText(id, value) {
    var node = document.getElementById(id);
    if (node) node.textContent = value;
  }

  function attachSettlementCheck() {
    var button = document.getElementById("settlement-check-button");
    if (!button) return;
    button.addEventListener("click", async function () {
      var result = document.getElementById("settlement-result");
      var summary = document.getElementById("settlement-summary");
      result.textContent = "";
      result.className = "form-message";
      button.disabled = true;
      try {
        var response = await fetch("/api/demo/settle", {
          method: "POST",
          headers: postHeaders({"X-Demo-Admin-Token": optionalValue("admin-token-settle")})
        });
        var data = await response.json();
        if (!response.ok) {
          result.textContent = data.detail || "結果確認に失敗しました。";
          result.className = "form-message error";
          return;
        }
        setText("settlement-checked", data.checked_count);
        setText("settlement-wins", data.settled_win_count);
        setText("settlement-losses", data.settled_loss_count);
        setText("settlement-hold", data.pending_count + data.unknown_count);
        setText("settlement-ws-confirmed", data.ws_confirmed_count || 0);
        setText("settlement-ws-unconfirmed", data.ws_unconfirmed_count || 0);
        setText("settlement-ws-conflict", data.ws_conflict_count || 0);
        setText("settlement-rest-only", data.rest_only_settled_count || 0);
        setText("settlement-reference-score", Number(data.total_payout || 0).toFixed(2));
        setText("settlement-balance", Number(data.balance || 0).toFixed(2));
        if (summary) summary.hidden = false;
        result.textContent = "結果確認が完了しました。ページを再読み込みすると最新の状態を表示します。";
        result.className = "form-message success";
      } catch (error) {
        result.textContent = "通信に失敗しました。";
        result.className = "form-message error";
      } finally {
        button.disabled = false;
      }
    });
  }

  function optionalValue(id) {
    var node = document.getElementById(id);
    return node && node.value ? node.value : null;
  }

  function updateBalance(value) {
    var balanceNode = document.getElementById("demo-balance");
    if (balanceNode) balanceNode.textContent = Number(value || 0).toFixed(2);
  }

  function attachDemoPointManagement() {
    var addForm = document.getElementById("demo-points-add-form");
    if (addForm) {
      addForm.addEventListener("submit", async function (event) {
        event.preventDefault();
        var result = document.getElementById("demo-points-add-result");
        result.textContent = "";
        try {
          var response = await fetch("/api/demo/wallet/add-points", {
            method: "POST",
            headers: postHeaders({"X-Demo-Admin-Token": optionalValue("admin-token-add")}),
            body: JSON.stringify({
              amount: document.getElementById("add-amount").value,
              reason: optionalValue("add-reason"),
              idempotency_key: optionalValue("add-idempotency-key")
            })
          });
          var data = await response.json();
          if (!response.ok) {
            result.textContent = data.detail || "デモポイント調整に失敗しました。";
            result.className = "form-message error";
            return;
          }
          updateBalance(data.balance);
          result.textContent = "デモポイント調整を記録しました。";
          result.className = "form-message success";
        } catch (error) {
          result.textContent = "通信に失敗しました。";
          result.className = "form-message error";
        }
      });
    }

    var resetForm = document.getElementById("demo-balance-reset-form");
    if (resetForm) {
      resetForm.addEventListener("submit", async function (event) {
        event.preventDefault();
        var result = document.getElementById("demo-balance-reset-result");
        result.textContent = "";
        try {
          var response = await fetch("/api/demo/wallet/reset", {
            method: "POST",
            headers: postHeaders({"X-Demo-Admin-Token": optionalValue("admin-token-reset")}),
            body: JSON.stringify({
              reason: optionalValue("reset-reason"),
              idempotency_key: optionalValue("reset-idempotency-key")
            })
          });
          var data = await response.json();
          if (!response.ok) {
            result.textContent = data.detail || "初期状態に戻せませんでした。";
            result.className = "form-message error";
            return;
          }
          updateBalance(data.balance);
          result.textContent = "初期状態に戻しました。";
          result.className = "form-message success";
        } catch (error) {
          result.textContent = "通信に失敗しました。";
          result.className = "form-message error";
        }
      });
    }
  }

  attachPredictionForm();
  attachSettlementCheck();
  attachDemoPointManagement();
  pollMarkets();
  pollRealtimeStatus();
  var seconds = Number(document.body.dataset.pollSeconds || "30");
  window.setInterval(function () {
    pollMarkets();
    pollRealtimeStatus();
  }, Math.max(15, Math.min(30, seconds)) * 1000);
})();
