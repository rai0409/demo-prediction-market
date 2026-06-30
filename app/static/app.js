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
          return;
        }
        result.textContent = "デモ参加を記録しました。デモ残高: " + Number(data.balance).toFixed(2);
      } catch (error) {
        result.textContent = "通信に失敗しました。";
      }
    });
    updateEstimator();
  }

  async function pollMarkets() {
    var list = document.getElementById("market-list");
    if (!list) return;
    try {
      var response = await fetch("/api/markets");
      if (!response.ok) return;
      var data = await response.json();
      if (!data.markets || !data.markets.length) return;
      document.getElementById("data-status").textContent = data.markets[0].data_source_status;
      document.getElementById("freshness").textContent = data.markets[0].fetched_at;
    } catch (error) {
      var status = document.getElementById("data-status");
      if (status) status.textContent = "last fetch failed";
    }
  }

  attachPredictionForm();
  pollMarkets();
  var seconds = Number(document.body.dataset.pollSeconds || "30");
  window.setInterval(pollMarkets, Math.max(15, Math.min(30, seconds)) * 1000);
})();
