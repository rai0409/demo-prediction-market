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

  function formatNumber(value) {
    var number = Number(value);
    if (value === null || value === "" || !Number.isFinite(number)) return "-";
    if (Math.abs(number) >= 1000000) return (number / 1000000).toFixed(2) + "M";
    if (Math.abs(number) >= 1000) return (number / 1000).toFixed(1) + "K";
    return number.toFixed(2);
  }

  function matchingNodes(root, attribute, value) {
    return Array.from(root.querySelectorAll("[" + attribute + "]")).filter(function (node) {
      return node.getAttribute(attribute) === value;
    });
  }

  function applyLiveMarketUpdate(root, market) {
    var probabilities = market.probabilities || {};
    Object.keys(probabilities).forEach(function (outcome) {
      var probability = Number(probabilities[outcome]);
      if (!Number.isFinite(probability)) return;
      matchingNodes(root, "data-live-probability", outcome).forEach(function (node) {
        node.textContent = (probability * 100).toFixed(1) + "%";
      });
      matchingNodes(root, "data-live-probability-bar", outcome).forEach(function (node) {
        node.style.width = Math.max(0, Math.min(100, probability * 100)) + "%";
      });
      matchingNodes(root, "data-live-option", outcome).forEach(function (node) {
        node.dataset.probability = String(probability);
        node.textContent = outcome + " · " + (probability * 100).toFixed(1) + "%";
      });
    });
    ["volume_24hr", "liquidity", "best_bid", "best_ask", "last_trade_price"].forEach(function (field) {
      root.querySelectorAll('[data-live-field="' + field + '"]').forEach(function (node) {
        node.textContent = formatNumber(market[field]);
      });
    });
    if (market.updated_at) {
      root.querySelectorAll('[data-live-field="updated_at"]').forEach(function (node) {
        node.textContent = formatMinute(market.updated_at);
      });
    }
    if (root.matches("[data-detail-market-id]")) updateEstimator();
  }

  var cardsInFlight = false;
  var cardsController = null;
  var detailInFlight = false;
  var detailController = null;

  async function pollVisibleMarketCards() {
    if (document.hidden || cardsInFlight) return;
    var cards = Array.from(document.querySelectorAll(".market-card[data-market-id]"));
    if (!cards.length) return;
    var ids = cards.slice(0, 50).map(function (card) { return card.dataset.marketId; });
    cardsInFlight = true;
    cardsController = new AbortController();
    try {
      var response = await fetch("/api/markets/updates?ids=" + encodeURIComponent(ids.join(",")), {
        signal: cardsController.signal
      });
      if (!response.ok) return;
      var data = await response.json();
      (data.markets || []).forEach(function (market) {
        cards.filter(function (card) { return card.dataset.marketId === market.market_id; }).forEach(function (card) {
          applyLiveMarketUpdate(card, market);
        });
      });
      var updatedValues = (data.markets || []).map(function (market) { return market.updated_at; }).filter(Boolean);
      var latest = document.getElementById("latest-update");
      if (latest && updatedValues.length) latest.textContent = formatMinute(updatedValues.sort().pop());
    } catch (error) {
      if (error.name !== "AbortError") return;
    } finally {
      cardsInFlight = false;
      cardsController = null;
    }
  }

  async function pollDetailMarket() {
    if (document.hidden || detailInFlight) return;
    var detail = document.querySelector("[data-detail-market-id]");
    if (!detail) return;
    detailInFlight = true;
    detailController = new AbortController();
    try {
      var response = await fetch("/api/markets/" + encodeURIComponent(detail.dataset.detailMarketId) + "/live", {
        signal: detailController.signal
      });
      if (!response.ok) return;
      applyLiveMarketUpdate(detail, await response.json());
    } catch (error) {
      if (error.name !== "AbortError") return;
    } finally {
      detailInFlight = false;
      detailController = null;
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
  pollVisibleMarketCards();
  pollDetailMarket();
  var quickSeconds = Number(document.body.dataset.quickRefreshSeconds || "5");
  var detailSeconds = Number(document.body.dataset.detailRefreshSeconds || "3");
  if (!Number.isFinite(quickSeconds)) quickSeconds = 5;
  if (!Number.isFinite(detailSeconds)) detailSeconds = 3;
  quickSeconds = Math.max(3, Math.min(30, quickSeconds));
  detailSeconds = Math.max(2, Math.min(15, detailSeconds));
  window.setInterval(pollVisibleMarketCards, quickSeconds * 1000);
  window.setInterval(pollDetailMarket, detailSeconds * 1000);
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) {
      if (cardsController) cardsController.abort();
      if (detailController) detailController.abort();
      return;
    }
    pollVisibleMarketCards();
    pollDetailMarket();
  });
})();
