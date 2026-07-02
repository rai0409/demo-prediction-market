(function () {
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
          headers: {"Content-Type": "application/json"},
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
    if (status === "live") return "LIVE Polymarket";
    if (status === "sample_fallback") return "Sample fallback";
    if (status === "live_failed_sample_fallback" || status === "live_empty_sample_fallback") {
      return "Live failed, sample fallback";
    }
    return status || "";
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
      document.getElementById("freshness").textContent = data.markets[0].fetched_at;
      var displayed = document.getElementById("displayed-count");
      var total = document.getElementById("total-count");
      if (displayed) displayed.textContent = data.count;
      if (total) total.textContent = data.total_market_count;
    } catch (error) {
      var status = document.getElementById("data-status");
      if (status) status.textContent = "last fetch failed";
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
        summary.textContent = "RESTのみ";
      } else if (data.live_market_update_count > 0) {
        summary.textContent = "WebSocket更新中";
      } else if (data.stale_market_update_count > 0) {
        summary.textContent = "WebSocket stale";
      } else {
        summary.textContent = "RESTのみ";
      }
    } catch (error) {
      summary.textContent = "RESTのみ";
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
        var response = await fetch("/api/demo/settle", {method: "POST"});
        var data = await response.json();
        if (!response.ok) {
          result.textContent = data.detail || "結果確認に失敗しました。";
          result.className = "form-message error";
          return;
        }
        setText("settlement-checked", data.checked_count);
        setText("settlement-wins", data.settled_win_count);
        setText("settlement-losses", data.settled_loss_count);
        setText("settlement-pending", data.pending_count + data.unknown_count);
        setText("settlement-payout", Number(data.total_payout || 0).toFixed(2));
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

  attachPredictionForm();
  attachSettlementCheck();
  pollMarkets();
  pollRealtimeStatus();
  var seconds = Number(document.body.dataset.pollSeconds || "30");
  window.setInterval(function () {
    pollMarkets();
    pollRealtimeStatus();
  }, Math.max(15, Math.min(30, seconds)) * 1000);
})();
