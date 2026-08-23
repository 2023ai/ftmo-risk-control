using System;
using System.Collections.Generic;
using System.Globalization;
using System.Text.Json;
using cAlgo.API;
using cAlgo.API.Internals;

namespace FtmoRiskControl
{
    [Robot(TimeZone = TimeZones.UTC, AccessRights = AccessRights.None)]
    public class RiskGuardBot : Robot
    {
        [Parameter("Risk API Base URL", DefaultValue = "http://127.0.0.1:8765")]
        public string RiskApiBaseUrl { get; set; }

        [Parameter("Risk API Token", DefaultValue = "")]
        public string RiskApiToken { get; set; }

        [Parameter("Account ID", DefaultValue = "ctrader-account-001")]
        public string AccountId { get; set; }

        [Parameter("Account Type", DefaultValue = "two_step")]
        public string AccountType { get; set; }

        [Parameter("Account Phase", DefaultValue = "evaluation")]
        public string AccountPhase { get; set; }

        [Parameter("Account Style", DefaultValue = "standard")]
        public string AccountStyle { get; set; }

        [Parameter("Initial Capital", DefaultValue = 100000)]
        public double InitialCapital { get; set; }

        [Parameter("Bootstrap Day Balance", DefaultValue = 100000)]
        public double BootstrapDayStartBalance { get; set; }

        [Parameter("Bootstrap High Balance", DefaultValue = 100000)]
        public double BootstrapHighestSettledBalance { get; set; }

        [Parameter("Estimated Costs", DefaultValue = 0)]
        public double EstimatedCostsPerTrade { get; set; }

        [Parameter("News Guard Seconds", DefaultValue = 5, MinValue = 1)]
        public int NewsGuardIntervalSeconds { get; set; }

        [Parameter("Label", DefaultValue = "FTMO-RISK")]
        public string Label { get; set; }

        private bool _unknownExecutionLock;

        private string UnknownExecutionLockKey
        {
            get { return "FTMO.RiskGuard.UnknownExecution." + AccountId; }
        }

        protected override void OnStart()
        {
            if (string.IsNullOrWhiteSpace(AccountId))
            {
                Print("RiskGuard: AccountId is required");
                Stop();
                return;
            }
            if (string.IsNullOrWhiteSpace(RiskApiToken))
            {
                Print("RiskGuard: Risk API token is required");
                Stop();
                return;
            }
            LoadUnknownExecutionLock();

            Timer.Start(NewsGuardIntervalSeconds);
            if (!SyncAccount())
            {
                Print(
                    "RiskGuard initialized fail-closed; verify the local risk API");
            }
        }

        protected override void OnTimer()
        {
            RunNewsGuard();
        }

        private static string DecimalText(double value)
        {
            return value.ToString("0.########", CultureInfo.InvariantCulture);
        }

        private static string Iso(DateTime value)
        {
            return value.ToUniversalTime().ToString(
                "yyyy-MM-dd'T'HH:mm:ss'Z'",
                CultureInfo.InvariantCulture);
        }

        private void LoadUnknownExecutionLock()
        {
            _unknownExecutionLock =
                LocalStorage.GetString(
                    UnknownExecutionLockKey,
                    LocalStorageScope.Type) == "1";
            if (_unknownExecutionLock)
            {
                Print(
                    "RiskGuard: unknown execution lock is active; "
                        + "manual reconciliation is required");
            }
        }

        private void LockUnknownExecution()
        {
            _unknownExecutionLock = true;
            LocalStorage.SetString(
                UnknownExecutionLockKey,
                "1",
                LocalStorageScope.Type);
            LocalStorage.Flush(LocalStorageScope.Type);
        }

        private string NextRequestId(string action)
        {
            return action + "-" + Guid.NewGuid().ToString("N");
        }

        private string SendRiskRequest(
            string path,
            string body,
            string requestId)
        {
            try
            {
                var request = new HttpRequest(
                    new Uri(RiskApiBaseUrl.TrimEnd('/') + path))
                {
                    Method = HttpMethod.Post,
                    Body = body
                };
                request.Headers.Add("Content-Type", "application/json");
                request.Headers.Add("X-Risk-Token", RiskApiToken);
                request.Headers.Add("X-Request-Id", requestId);

                var response = Http.Send(request);
                if (!response.IsSuccessful)
                {
                    Print(
                        "Risk API HTTP error: path={0} status={1}",
                        path,
                        response.StatusCode);
                    return null;
                }
                return response.Body;
            }
            catch (Exception exception)
            {
                Print(
                    "Risk API exception: path={0} error={1}",
                    path,
                    exception.Message);
                return null;
            }
        }

        private double CurrentOpenRisk()
        {
            double total = 0;
            foreach (var position in Positions)
            {
                if (!position.StopLoss.HasValue)
                    return InitialCapital;

                var symbol = Symbols.GetSymbol(position.SymbolName);
                if (symbol == null)
                    return InitialCapital;

                var currentPrice = position.TradeType == TradeType.Buy
                    ? symbol.Bid
                    : symbol.Ask;
                var stopPips = Math.Abs(
                    currentPrice - position.StopLoss.Value)
                    / symbol.PipSize;
                total += Math.Max(
                    0,
                    symbol.AmountRisked(
                        position.VolumeInUnits,
                        stopPips));
            }
            foreach (var order in PendingOrders)
            {
                if (!order.StopLoss.HasValue)
                    return InitialCapital;

                var symbol = Symbols.GetSymbol(order.SymbolName);
                if (symbol == null)
                    return InitialCapital;

                var stopPips = Math.Abs(
                    order.TargetPrice - order.StopLoss.Value)
                    / symbol.PipSize;
                total += Math.Max(
                    0,
                    symbol.AmountRisked(
                        order.VolumeInUnits,
                        stopPips));
            }
            return total;
        }

        private bool SyncAccount()
        {
            var payload = new Dictionary<string, object>
            {
                ["account_id"] = AccountId,
                ["account_type"] = AccountType,
                ["phase"] = AccountPhase,
                ["style"] = AccountStyle,
                ["initial_capital"] = DecimalText(InitialCapital),
                ["day_start_balance"] =
                    DecimalText(BootstrapDayStartBalance),
                ["highest_settled_balance"] =
                    DecimalText(BootstrapHighestSettledBalance),
                ["balance"] = DecimalText(Account.Balance),
                ["equity"] = DecimalText(Account.Equity),
                ["current_open_risk"] = DecimalText(CurrentOpenRisk()),
                ["as_of"] = Iso(Server.TimeInUtc)
            };
            return SendRiskRequest(
                "/v1/account-sync",
                JsonSerializer.Serialize(payload),
                NextRequestId("sync")) != null;
        }

        private bool Evaluate(
            Dictionary<string, object> tradeRequest,
            string requestId,
            out string response)
        {
            response = null;
            if (!SyncAccount())
                return false;

            var payload = new Dictionary<string, object>
            {
                ["account_id"] = AccountId,
                ["request"] = tradeRequest
            };
            response = SendRiskRequest(
                "/v1/evaluate",
                JsonSerializer.Serialize(payload),
                requestId);
            if (string.IsNullOrWhiteSpace(response))
                return false;

            try
            {
                using var document = JsonDocument.Parse(response);
                return document.RootElement
                    .GetProperty("decision")
                    .GetProperty("allowed")
                    .GetBoolean();
            }
            catch (Exception exception)
            {
                Print("RiskGuard: invalid risk response: {0}", exception.Message);
                return false;
            }
        }

        private void ReportExecution(
            string requestId,
            string action,
            string symbolName,
            string outcome,
            string platformStatus,
            string platformOrderId)
        {
            if (outcome == "unknown")
                LockUnknownExecution();
            var payload = new Dictionary<string, object>
            {
                ["account_id"] = AccountId,
                ["request_id"] = requestId,
                ["outcome"] = outcome,
                ["action"] = action,
                ["symbol"] = symbolName,
                ["occurred_at"] = Iso(Server.TimeInUtc),
                ["platform_status"] = platformStatus ?? string.Empty,
                ["platform_order_id"] = platformOrderId ?? string.Empty
            };
            var response = SendRiskRequest(
                "/v1/execution-result",
                JsonSerializer.Serialize(payload),
                NextRequestId("execution"));
            if (response == null)
            {
                Print(
                    "RiskGuard: execution audit unresolved request_id={0}",
                    requestId);
            }
        }

        private static string TradeOutcome(TradeResult result)
        {
            if (result == null)
                return "unknown";
            if (result.IsSuccessful)
                return "success";
            if (!result.Error.HasValue
                || result.Error.Value == ErrorCode.Timeout
                || result.Error.Value == ErrorCode.Disconnected
                || result.Error.Value == ErrorCode.TechnicalError)
            {
                return "unknown";
            }
            return "failure";
        }

        public TradeResult TryExecuteMarket(
            TradeType tradeType,
            double volumeInUnits,
            double stopLossPips,
            double takeProfitPips,
            string ideaId)
        {
            if (_unknownExecutionLock)
            {
                Print(
                    "RiskGuard: new risk blocked by unknown execution lock");
                return null;
            }
            if (volumeInUnits <= 0 || stopLossPips <= 0)
                return null;

            var normalizedVolume = Symbol.NormalizeVolumeInUnits(
                volumeInUnits,
                RoundingMode.Down);
            if (normalizedVolume < Symbol.VolumeInUnitsMin
                || normalizedVolume > Symbol.VolumeInUnitsMax)
                return null;

            var entry = tradeType == TradeType.Buy
                ? Symbol.Ask
                : Symbol.Bid;
            var stopPrice = tradeType == TradeType.Buy
                ? entry - stopLossPips * Symbol.PipSize
                : entry + stopLossPips * Symbol.PipSize;
            var lossPerUnit = Symbol.AmountRisked(1, stopLossPips);
            if (lossPerUnit <= 0)
                return null;

            var requestId = NextRequestId("open");
            var request = new Dictionary<string, object>
            {
                ["symbol"] = SymbolName,
                ["action"] = "open",
                ["requested_at"] = Iso(Server.TimeInUtc),
                ["volume"] = DecimalText(normalizedVolume),
                ["entry_price"] = DecimalText(entry),
                ["stop_loss"] = DecimalText(stopPrice),
                ["loss_per_volume_unit"] = DecimalText(lossPerUnit),
                ["estimated_costs"] =
                    DecimalText(EstimatedCostsPerTrade),
                ["is_risk_increasing"] = true,
                ["idea_id"] = ideaId
            };

            if (!Evaluate(request, requestId, out var response))
            {
                Print("RiskGuard rejected open: {0}", response ?? "no response");
                return null;
            }

            var result = ExecuteMarketOrder(
                tradeType,
                SymbolName,
                normalizedVolume,
                Label,
                stopLossPips,
                takeProfitPips,
                ideaId);
            if (result == null)
            {
                ReportExecution(
                    requestId,
                    "open",
                    SymbolName,
                    "unknown",
                    "null-trade-result",
                    string.Empty);
                Print(
                    "RiskGuard: market order result was unknown; "
                        + "new risk remains blocked by operator policy");
                return null;
            }
            ReportExecution(
                requestId,
                "open",
                SymbolName,
                TradeOutcome(result),
                result.Error.ToString(),
                result.Position == null
                    ? string.Empty
                    : result.Position.Id.ToString(
                        CultureInfo.InvariantCulture));
            return result;
        }

        public TradeResult TryExecuteBuy(
            double volumeInUnits,
            double stopLossPips,
            double takeProfitPips,
            string ideaId)
        {
            return TryExecuteMarket(
                TradeType.Buy,
                volumeInUnits,
                stopLossPips,
                takeProfitPips,
                ideaId);
        }

        public TradeResult TryExecuteSell(
            double volumeInUnits,
            double stopLossPips,
            double takeProfitPips,
            string ideaId)
        {
            return TryExecuteMarket(
                TradeType.Sell,
                volumeInUnits,
                stopLossPips,
                takeProfitPips,
                ideaId);
        }

        public TradeResult TryClose(Position position)
        {
            if (position == null)
                return null;
            var requestId = NextRequestId("close");
            var request = new Dictionary<string, object>
            {
                ["symbol"] = position.SymbolName,
                ["action"] = "close",
                ["requested_at"] = Iso(Server.TimeInUtc),
                ["is_risk_increasing"] = false
            };
            if (!Evaluate(request, requestId, out var response))
            {
                Print("RiskGuard rejected close: {0}", response ?? "no response");
                return null;
            }

            var result = ClosePosition(position);
            if (result == null)
            {
                ReportExecution(
                    requestId,
                    "close",
                    position.SymbolName,
                    "unknown",
                    "null-trade-result",
                    position.Id.ToString(CultureInfo.InvariantCulture));
                return null;
            }
            ReportExecution(
                requestId,
                "close",
                position.SymbolName,
                TradeOutcome(result),
                result.Error.ToString(),
                position.Id.ToString(CultureInfo.InvariantCulture));
            return result;
        }

        public TradeResult TryClosePartial(
            Position position,
            double volumeInUnits)
        {
            if (position == null || volumeInUnits <= 0)
                return null;

            var positionSymbol = Symbols.GetSymbol(position.SymbolName);
            if (positionSymbol == null)
                return null;

            var normalizedVolume = positionSymbol.NormalizeVolumeInUnits(
                volumeInUnits,
                RoundingMode.Down);
            if (normalizedVolume < positionSymbol.VolumeInUnitsMin
                || normalizedVolume > position.VolumeInUnits)
                return null;

            var requestId = NextRequestId("close-partial");
            var request = new Dictionary<string, object>
            {
                ["symbol"] = position.SymbolName,
                ["action"] = "close",
                ["requested_at"] = Iso(Server.TimeInUtc),
                ["volume"] = DecimalText(normalizedVolume),
                ["is_risk_increasing"] = false
            };
            if (!Evaluate(request, requestId, out var response))
            {
                Print(
                    "RiskGuard rejected partial close: "
                        + (response ?? "no response"));
                return null;
            }

            var result = ClosePosition(position, normalizedVolume);
            if (result == null)
            {
                ReportExecution(
                    requestId,
                    "close",
                    position.SymbolName,
                    "unknown",
                    "null-trade-result",
                    position.Id.ToString(CultureInfo.InvariantCulture));
                return null;
            }
            ReportExecution(
                requestId,
                "close",
                position.SymbolName,
                TradeOutcome(result),
                result.Error.ToString(),
                position.Id.ToString(CultureInfo.InvariantCulture));
            return result;
        }

        public TradeResult TryModifyPosition(
            Position position,
            double? stopLossPrice,
            double? takeProfitPrice,
            bool isRiskIncreasing)
        {
            if (position == null)
                return null;
            if (isRiskIncreasing && !stopLossPrice.HasValue)
                return null;

            var positionSymbol = Symbols.GetSymbol(position.SymbolName);
            if (positionSymbol == null)
                return null;
            if (stopLossPrice.HasValue
                && ((position.TradeType == TradeType.Buy
                        && stopLossPrice.Value >= position.EntryPrice)
                    || (position.TradeType == TradeType.Sell
                        && stopLossPrice.Value <= position.EntryPrice)))
            {
                return null;
            }
            var currentRisk = position.StopLoss.HasValue
                ? positionSymbol.AmountRisked(
                    position.VolumeInUnits,
                    Math.Abs(position.EntryPrice - position.StopLoss.Value)
                        / positionSymbol.PipSize)
                : InitialCapital;
            var newRisk = stopLossPrice.HasValue
                ? positionSymbol.AmountRisked(
                    position.VolumeInUnits,
                    Math.Abs(position.EntryPrice - stopLossPrice.Value)
                        / positionSymbol.PipSize)
                : InitialCapital;
            var additionalRisk = Math.Max(0, newRisk - currentRisk);
            var effectiveRiskIncreasing = additionalRisk > 0.01;
            if (effectiveRiskIncreasing && _unknownExecutionLock)
            {
                Print(
                    "RiskGuard: risk-increasing modification blocked by "
                        + "unknown execution lock");
                return null;
            }

            var requestId = NextRequestId("modify");
            var request = new Dictionary<string, object>
            {
                ["symbol"] = position.SymbolName,
                ["action"] = "modify",
                ["requested_at"] = Iso(Server.TimeInUtc),
                ["stop_loss"] = stopLossPrice.HasValue
                    ? DecimalText(stopLossPrice.Value)
                    : null,
                ["additional_risk"] = DecimalText(additionalRisk),
                ["is_risk_increasing"] = effectiveRiskIncreasing
            };
            if (!Evaluate(request, requestId, out var response))
            {
                Print(
                    "RiskGuard rejected position modification: "
                        + (response ?? "no response"));
                return null;
            }

            var result = ModifyPosition(
                position,
                stopLossPrice,
                takeProfitPrice,
                ProtectionType.Absolute);
            if (result == null)
            {
                ReportExecution(
                    requestId,
                    "modify",
                    position.SymbolName,
                    "unknown",
                    "null-trade-result",
                    position.Id.ToString(CultureInfo.InvariantCulture));
                return null;
            }
            ReportExecution(
                requestId,
                "modify",
                position.SymbolName,
                TradeOutcome(result),
                result.Error.ToString(),
                position.Id.ToString(CultureInfo.InvariantCulture));
            return result;
        }

        public TradeResult TryCancelPendingOrder(PendingOrder order)
        {
            if (order == null)
                return null;

            var requestId = NextRequestId("cancel");
            var request = new Dictionary<string, object>
            {
                ["symbol"] = order.SymbolName,
                ["action"] = "cancel",
                ["requested_at"] = Iso(Server.TimeInUtc),
                ["is_risk_increasing"] = false
            };
            if (!Evaluate(request, requestId, out var response))
            {
                Print(
                    "RiskGuard rejected pending cancellation: "
                        + (response ?? "no response"));
                return null;
            }

            var result = CancelPendingOrder(order);
            if (result == null)
            {
                ReportExecution(
                    requestId,
                    "cancel",
                    order.SymbolName,
                    "unknown",
                    "null-trade-result",
                    order.Id.ToString(CultureInfo.InvariantCulture));
                return null;
            }
            ReportExecution(
                requestId,
                "cancel",
                order.SymbolName,
                TradeOutcome(result),
                result.Error.ToString(),
                order.Id.ToString(CultureInfo.InvariantCulture));
            return result;
        }

        private JsonElement? GuardStatus(string path, string symbolName)
        {
            var payload = new Dictionary<string, object>
            {
                ["account_id"] = AccountId,
                ["symbol"] = symbolName,
                ["now"] = Iso(Server.TimeInUtc)
            };
            var response = SendRiskRequest(
                path,
                JsonSerializer.Serialize(payload),
                NextRequestId("news"));
            if (string.IsNullOrWhiteSpace(response))
                return null;

            try
            {
                using var document = JsonDocument.Parse(response);
                return document.RootElement.Clone();
            }
            catch (Exception exception)
            {
                Print("RiskGuard: invalid news response: {0}", exception.Message);
                return null;
            }
        }

        private static bool Flag(JsonElement status, string name)
        {
            return status.TryGetProperty(name, out var value)
                && value.ValueKind == JsonValueKind.True;
        }

        private void RunNewsGuard()
        {
            if (!SyncAccount())
                return;

            var positions = new List<Position>();
            foreach (var position in Positions)
                positions.Add(position);

            foreach (var position in positions)
            {
                var newsStatus = GuardStatus(
                    "/v1/news-status",
                    position.SymbolName);
                var marketStatus = GuardStatus(
                    "/v1/market-status",
                    position.SymbolName);
                var forceFlat =
                    (newsStatus.HasValue
                        && Flag(newsStatus.Value, "force_flat"))
                    || (marketStatus.HasValue
                        && Flag(marketStatus.Value, "force_flat"));
                if (forceFlat)
                {
                    TryClose(position);
                }
                else if (!newsStatus.HasValue
                    || !marketStatus.HasValue
                    || Flag(newsStatus.Value, "emergency_alert")
                    || Flag(marketStatus.Value, "emergency_alert"))
                {
                    Notifications.ShowPopup(
                        "FTMO RiskGuard",
                        "Position status is unresolved or restricted: "
                            + position.SymbolName,
                        PopupNotificationState.Error);
                }
            }

            var pendingOrders = new List<PendingOrder>();
            foreach (var order in PendingOrders)
                pendingOrders.Add(order);

            foreach (var order in pendingOrders)
            {
                var newsStatus = GuardStatus(
                    "/v1/news-status",
                    order.SymbolName);
                var marketStatus = GuardStatus(
                    "/v1/market-status",
                    order.SymbolName);
                var cancelPending =
                    (newsStatus.HasValue
                        && Flag(newsStatus.Value, "cancel_pending"))
                    || (marketStatus.HasValue
                        && Flag(marketStatus.Value, "cancel_pending"));
                if (cancelPending)
                {
                    TryCancelPendingOrder(order);
                }
                else if (!newsStatus.HasValue || !marketStatus.HasValue)
                {
                    Print(
                        "RiskGuard: pending order status unresolved id={0}",
                        order.Id);
                }
            }
        }
    }
}
