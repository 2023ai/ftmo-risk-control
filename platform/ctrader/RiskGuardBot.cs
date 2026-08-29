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

        [Parameter("Account Credential", DefaultValue = "")]
        public string AccountCredential { get; set; }

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

        [Parameter("Risk API Timeout (ms)", DefaultValue = 3000, MinValue = 250)]
        public int RiskApiTimeoutMilliseconds { get; set; }

        [Parameter("News Guard Seconds", DefaultValue = 5, MinValue = 1)]
        public int NewsGuardIntervalSeconds { get; set; }

        [Parameter("Label", DefaultValue = "FTMO-RISK")]
        public string Label { get; set; }

        private bool _unknownExecutionLock;
        private long _lastTimestampMilliseconds;

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
            if (string.IsNullOrWhiteSpace(AccountCredential))
            {
                Print("RiskGuard: account credential is required");
                Stop();
                return;
            }
            if (RiskApiTimeoutMilliseconds <= 0)
            {
                Print("RiskGuard: Risk API timeout must be positive");
                Stop();
                return;
            }
            if (!IsFinite(InitialCapital)
                || !IsFinite(BootstrapDayStartBalance)
                || !IsFinite(BootstrapHighestSettledBalance)
                || !IsFinite(EstimatedCostsPerTrade)
                || InitialCapital <= 0
                || BootstrapDayStartBalance <= 0
                || BootstrapHighestSettledBalance < InitialCapital
                || EstimatedCostsPerTrade < 0
                || NewsGuardIntervalSeconds <= 0)
            {
                Print("RiskGuard: numeric parameters are invalid");
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

        private static bool IsFinite(double value)
        {
            return !double.IsNaN(value) && !double.IsInfinity(value);
        }

        private static bool IsBoolean(JsonElement root, string propertyName)
        {
            return root.TryGetProperty(propertyName, out var value)
                && (value.ValueKind == JsonValueKind.True
                    || value.ValueKind == JsonValueKind.False);
        }

        private static bool HasString(
            JsonElement root,
            string propertyName,
            string expected)
        {
            return root.TryGetProperty(propertyName, out var value)
                && value.ValueKind == JsonValueKind.String
                && value.GetString() == expected;
        }

        private static bool IsTrue(JsonElement root, string propertyName)
        {
            return root.TryGetProperty(propertyName, out var value)
                && value.ValueKind == JsonValueKind.True;
        }

        private static bool AccountSyncAccepted(
            string response,
            string accountId,
            string requestId)
        {
            try
            {
                using var document = JsonDocument.Parse(response);
                var root = document.RootElement;
                return IsTrue(root, "ok")
                    && HasString(root, "account_id", accountId)
                    && HasString(root, "request_id", requestId);
            }
            catch
            {
                return false;
            }
        }

        private static bool ExecutionReportAccepted(
            string response,
            string accountId,
            string requestId)
        {
            try
            {
                using var document = JsonDocument.Parse(response);
                var root = document.RootElement;
                return IsTrue(root, "ok")
                    && IsTrue(root, "execution_recorded")
                    && HasString(root, "account_id", accountId)
                    && HasString(root, "request_id", requestId);
            }
            catch
            {
                return false;
            }
        }

        private string Iso(DateTime value)
        {
            var milliseconds = value.ToUniversalTime().Ticks
                / TimeSpan.TicksPerMillisecond;
            if (milliseconds <= _lastTimestampMilliseconds)
                milliseconds = _lastTimestampMilliseconds + 1;
            _lastTimestampMilliseconds = milliseconds;
            return new DateTime(
                milliseconds * TimeSpan.TicksPerMillisecond,
                DateTimeKind.Utc).ToString(
                "yyyy-MM-dd'T'HH:mm:ss.fff'Z'",
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

        private void ClearUnknownExecutionLock()
        {
            _unknownExecutionLock = false;
            LocalStorage.SetString(
                UnknownExecutionLockKey,
                "0",
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
                    Body = body,
                    Timeout = TimeSpan.FromMilliseconds(
                        RiskApiTimeoutMilliseconds)
                };
                request.Headers.Add("Content-Type", "application/json");
                request.Headers.Add(
                    "X-Account-Credential",
                    AccountCredential);
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
                if (!IsFinite(currentPrice)
                    || !IsFinite(position.StopLoss.Value)
                    || currentPrice <= 0
                    || symbol.PipSize <= 0)
                    return InitialCapital;
                if ((position.TradeType == TradeType.Buy
                        && position.StopLoss.Value >= currentPrice)
                    || (position.TradeType == TradeType.Sell
                        && position.StopLoss.Value <= currentPrice))
                    return InitialCapital;
                var stopPips = Math.Abs(
                    currentPrice - position.StopLoss.Value)
                    / symbol.PipSize;
                var positionRisk = symbol.AmountRisked(
                    position.VolumeInUnits,
                    stopPips);
                if (!IsFinite(positionRisk) || positionRisk < 0)
                    return InitialCapital;
                total += positionRisk;
                if (!IsFinite(total) || total < 0)
                    return InitialCapital;
            }
            foreach (var order in PendingOrders)
            {
                if (!order.StopLoss.HasValue)
                    return InitialCapital;

                var symbol = Symbols.GetSymbol(order.SymbolName);
                if (symbol == null)
                    return InitialCapital;
                if (!IsFinite(order.TargetPrice)
                    || !IsFinite(order.StopLoss.Value)
                    || symbol.PipSize <= 0)
                    return InitialCapital;
                if ((order.TradeType == TradeType.Buy
                        && order.StopLoss.Value >= order.TargetPrice)
                    || (order.TradeType == TradeType.Sell
                        && order.StopLoss.Value <= order.TargetPrice))
                    return InitialCapital;

                var stopPips = Math.Abs(
                    order.TargetPrice - order.StopLoss.Value)
                    / symbol.PipSize;
                var orderRisk = symbol.AmountRisked(
                    order.VolumeInUnits,
                    stopPips);
                if (!IsFinite(orderRisk) || orderRisk < 0)
                    return InitialCapital;
                total += orderRisk;
                if (!IsFinite(total) || total < 0)
                    return InitialCapital;
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
                ["open_positions_count"] = Positions.Count,
                ["pending_orders_count"] = PendingOrders.Count,
                ["as_of"] = Iso(Server.TimeInUtc)
            };
            var requestId = NextRequestId("sync");
            var response = SendRiskRequest(
                "/v1/account-sync",
                JsonSerializer.Serialize(payload),
                requestId);
            return response != null
                && AccountSyncAccepted(response, AccountId, requestId);
        }

        private bool Evaluate(
            Dictionary<string, object> tradeRequest,
            string requestId,
            out string response,
            bool riskIncreasing)
        {
            response = null;
            var synced = SyncAccount();
            if (riskIncreasing && !synced)
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
                var root = document.RootElement;
                if (!IsTrue(root, "ok")
                    || !HasString(root, "account_id", AccountId)
                    || !HasString(root, "request_id", requestId)
                    || !root.TryGetProperty("decision", out var decision)
                    || decision.ValueKind != JsonValueKind.Object
                    || !IsBoolean(decision, "allowed"))
                    return false;
                if (decision.TryGetProperty("code", out var code)
                    && code.ValueKind == JsonValueKind.String
                    && code.GetString() == "REJECT_UNKNOWN_EXECUTION")
                {
                    LockUnknownExecution();
                }
                return decision.GetProperty("allowed").GetBoolean();
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
            var body = JsonSerializer.Serialize(payload);
            var reportRequestId = NextRequestId("execution");
            var response = SendRiskRequest(
                "/v1/execution-result",
                body,
                reportRequestId);
            if (!ExecutionReportAccepted(response, AccountId, requestId))
            {
                // The body keeps the original request_id, so this retry
                // remains idempotent if the first response was lost.
                response = SendRiskRequest(
                    "/v1/execution-result",
                    body,
                    NextRequestId("execution-retry"));
            }
            if (!ExecutionReportAccepted(response, AccountId, requestId))
            {
                LockUnknownExecution();
                Print(
                    "RiskGuard: execution audit unresolved request_id={0}",
                    requestId);
            }
        }

        private bool RefreshUnknownExecutionLock()
        {
            var payload = new Dictionary<string, object>
            {
                ["account_id"] = AccountId
            };
            var requestId = NextRequestId("execution-status");
            var response = SendRiskRequest(
                "/v1/execution-status",
                JsonSerializer.Serialize(payload),
                requestId);
            if (string.IsNullOrWhiteSpace(response))
                return false;

            try
            {
                using var document = JsonDocument.Parse(response);
                var root = document.RootElement;
                if (!IsTrue(root, "ok")
                    || !HasString(root, "account_id", AccountId)
                    || !HasString(root, "request_id", requestId)
                    || !IsBoolean(root, "risk_increase_blocked")
                    || !IsBoolean(root, "reconciliation_complete"))
                    return false;
                var blocked = root.GetProperty("risk_increase_blocked").GetBoolean();
                var reconciliationComplete = root
                    .GetProperty("reconciliation_complete")
                    .GetBoolean();
                if (blocked || !reconciliationComplete)
                {
                    LockUnknownExecution();
                    return true;
                }
                ClearUnknownExecutionLock();
                return true;
            }
            catch (Exception exception)
            {
                Print(
                    "RiskGuard: invalid execution status response: {0}",
                    exception.Message);
            }
            return false;
        }

        private static string TradeOutcome(TradeResult result)
        {
            if (result == null)
                return "unknown";
            if (result.IsSuccessful)
                return "success";
            if (!result.Error.HasValue)
            {
                return "unknown";
            }
            switch (result.Error.Value)
            {
                case ErrorCode.BadVolume:
                case ErrorCode.NoMoney:
                case ErrorCode.MarketClosed:
                case ErrorCode.EntityNotFound:
                case ErrorCode.UnknownSymbol:
                case ErrorCode.InvalidStopLossTakeProfit:
                case ErrorCode.InvalidRequest:
                case ErrorCode.NoTradingPermission:
                    return "failure";
                default:
                    // Unknown and transport-like errors must retain the
                    // reservation until the platform order is reconciled.
                    return "unknown";
            }
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
                RefreshUnknownExecutionLock();
                if (_unknownExecutionLock)
                {
                    Print(
                        "RiskGuard: new risk blocked by unknown execution lock");
                    return null;
                }
            }
            if (!IsFinite(volumeInUnits)
                || !IsFinite(stopLossPips)
                || !IsFinite(takeProfitPips)
                || volumeInUnits <= 0
                || stopLossPips <= 0
                || takeProfitPips < 0)
                return null;

            var normalizedVolume = Symbol.NormalizeVolumeInUnits(
                volumeInUnits,
                RoundingMode.Down);
            if (!IsFinite(normalizedVolume)
                || normalizedVolume < Symbol.VolumeInUnitsMin
                || normalizedVolume > Symbol.VolumeInUnitsMax)
                return null;

            var entry = tradeType == TradeType.Buy
                ? Symbol.Ask
                : Symbol.Bid;
            var stopPrice = tradeType == TradeType.Buy
                ? entry - stopLossPips * Symbol.PipSize
                : entry + stopLossPips * Symbol.PipSize;
            if (!IsFinite(entry)
                || !IsFinite(stopPrice)
                || entry <= 0
                || stopPrice <= 0
                || Symbol.PipSize <= 0)
                return null;
            var lossPerUnit = Symbol.AmountRisked(1, stopLossPips);
            if (!IsFinite(lossPerUnit) || lossPerUnit <= 0)
                return null;

            var requestId = NextRequestId("open");
            var request = new Dictionary<string, object>
            {
                ["symbol"] = SymbolName,
                ["action"] = "open",
                ["side"] = tradeType == TradeType.Buy ? "buy" : "sell",
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

            if (!Evaluate(request, requestId, out var response, true))
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
            if (!Evaluate(request, requestId, out var response, false))
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
            if (position == null
                || !IsFinite(volumeInUnits)
                || volumeInUnits <= 0)
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
            if (!Evaluate(request, requestId, out var response, false))
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
            if ((stopLossPrice.HasValue && !IsFinite(stopLossPrice.Value))
                || (takeProfitPrice.HasValue
                    && !IsFinite(takeProfitPrice.Value)))
                return null;
            if (isRiskIncreasing && !stopLossPrice.HasValue)
                return null;

            var positionSymbol = Symbols.GetSymbol(position.SymbolName);
            if (positionSymbol == null)
                return null;
            if (!stopLossPrice.HasValue)
            {
                if (!position.StopLoss.HasValue || isRiskIncreasing)
                    return null;
                // A missing stop means "keep the current stop" for a TP-only change.
                stopLossPrice = position.StopLoss.Value;
            }
            var currentPrice = position.TradeType == TradeType.Buy
                ? positionSymbol.Bid
                : positionSymbol.Ask;
            if (!IsFinite(currentPrice)
                || currentPrice <= 0
                || positionSymbol.PipSize <= 0)
                return null;
            if (position.StopLoss.HasValue
                && ((position.TradeType == TradeType.Buy
                        && position.StopLoss.Value >= currentPrice)
                    || (position.TradeType == TradeType.Sell
                        && position.StopLoss.Value <= currentPrice)))
                return null;
            if ((position.TradeType == TradeType.Buy
                    && stopLossPrice.Value >= currentPrice)
                || (position.TradeType == TradeType.Sell
                    && stopLossPrice.Value <= currentPrice))
            {
                return null;
            }
            var currentRisk = position.StopLoss.HasValue
                ? positionSymbol.AmountRisked(
                    position.VolumeInUnits,
                    Math.Abs(currentPrice - position.StopLoss.Value)
                        / positionSymbol.PipSize)
                : InitialCapital;
            var newRisk = positionSymbol.AmountRisked(
                position.VolumeInUnits,
                Math.Abs(currentPrice - stopLossPrice.Value)
                    / positionSymbol.PipSize);
            if (!IsFinite(currentRisk)
                || !IsFinite(newRisk)
                || currentRisk < 0
                || newRisk < 0)
                return null;
            var additionalRisk = Math.Max(0, newRisk - currentRisk);
            var effectiveRiskIncreasing = additionalRisk > 0.00000001;
            if (effectiveRiskIncreasing && _unknownExecutionLock)
            {
                RefreshUnknownExecutionLock();
            }
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
                ["side"] = position.TradeType == TradeType.Buy ? "buy" : "sell",
                ["requested_at"] = Iso(Server.TimeInUtc),
                ["stop_loss"] = DecimalText(stopLossPrice.Value),
                ["additional_risk"] = DecimalText(additionalRisk),
                ["is_risk_increasing"] = effectiveRiskIncreasing
            };
            if (!Evaluate(request, requestId, out var response, effectiveRiskIncreasing))
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
            if (!Evaluate(request, requestId, out var response, false))
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

        private JsonElement? GuardStatus(
            string path,
            string symbolName)
        {
            var requestId = NextRequestId("status");
            var payload = new Dictionary<string, object>
            {
                ["account_id"] = AccountId,
                ["symbol"] = symbolName,
                ["now"] = Iso(Server.TimeInUtc)
            };
            var response = SendRiskRequest(
                path,
                JsonSerializer.Serialize(payload),
                requestId);
            if (string.IsNullOrWhiteSpace(response))
                return null;

            try
            {
                using var document = JsonDocument.Parse(response);
                var root = document.RootElement;
                if (!IsTrue(root, "ok")
                    || !HasString(root, "account_id", AccountId)
                    || !HasString(root, "request_id", requestId)
                    || !HasString(root, "symbol", symbolName.ToUpperInvariant())
                    || !root.TryGetProperty("open_blocked", out var openBlocked)
                    || (openBlocked.ValueKind != JsonValueKind.True
                        && openBlocked.ValueKind != JsonValueKind.False)
                    || !root.TryGetProperty("force_flat", out var forceFlat)
                    || (forceFlat.ValueKind != JsonValueKind.True
                        && forceFlat.ValueKind != JsonValueKind.False)
                    || !root.TryGetProperty(
                        "cancel_pending",
                        out var cancelPending)
                    || (cancelPending.ValueKind != JsonValueKind.True
                        && cancelPending.ValueKind != JsonValueKind.False)
                    || !root.TryGetProperty(
                        "emergency_alert",
                        out var emergencyAlert)
                    || (emergencyAlert.ValueKind != JsonValueKind.True
                        && emergencyAlert.ValueKind != JsonValueKind.False))
                {
                    return null;
                }
                return root.Clone();
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
            {
                Print(
                    "RiskGuard: account sync failed; continuing defensive guard");
            }
            RefreshUnknownExecutionLock();

            var positions = new List<Position>();
            foreach (var position in Positions)
                positions.Add(position);
            var pendingOrders = new List<PendingOrder>();
            foreach (var order in PendingOrders)
                pendingOrders.Add(order);

            var statusBySymbol =
                new Dictionary<string, (JsonElement? News, JsonElement? Market)>(
                    StringComparer.OrdinalIgnoreCase);
            var symbols = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            foreach (var position in positions)
                symbols.Add(position.SymbolName);
            foreach (var order in pendingOrders)
                symbols.Add(order.SymbolName);
            foreach (var symbol in symbols)
            {
                statusBySymbol[symbol] = (
                    GuardStatus("/v1/news-status", symbol),
                    GuardStatus("/v1/market-status", symbol));
            }

            foreach (var position in positions)
            {
                var status = statusBySymbol[position.SymbolName];
                var newsStatus = status.News;
                var marketStatus = status.Market;
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

            foreach (var order in pendingOrders)
            {
                var status = statusBySymbol[order.SymbolName];
                var newsStatus = status.News;
                var marketStatus = status.Market;
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
