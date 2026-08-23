#property strict
#property version "1.1"
#property description "Stateful FTMO risk API guard for MT5"

#include <Trade/Trade.mqh>

CTrade Trade;

input string RiskApiBaseUrl = "http://127.0.0.1:8765";
input string AccountCredential = "";
input string AccountId = "mt5-account-001";
input string AccountType = "two_step";
input string AccountPhase = "evaluation";
input string AccountStyle = "standard";
input double InitialCapital = 100000.0;
input double BootstrapDayStartBalance = 100000.0;
input double BootstrapHighestSettledBalance = 100000.0;
input double EstimatedCostsPerTrade = 0.0;
input int RiskApiTimeoutMs = 3000;
input int NewsGuardIntervalSeconds = 5;
input ulong MagicNumber = 26082201;

bool UnknownExecutionLock = false;

string UnknownLockName()
{
   return "FTMO.RiskGuard.UnknownExecution." + AccountId;
}

void LoadUnknownExecutionLock()
{
   UnknownExecutionLock = GlobalVariableCheck(UnknownLockName());
   if(UnknownExecutionLock)
   {
      Print(
         "RiskGuard: unknown execution lock is active; "
         "manual reconciliation is required");
   }
}

string JsonQuote(string value)
{
   StringReplace(value, "\\", "\\\\");
   StringReplace(value, "\"", "\\\"");
   return "\"" + value + "\"";
}

string IsoUtc(datetime value)
{
   string stamp = TimeToString(value, TIME_DATE | TIME_SECONDS);
   StringReplace(stamp, ".", "-");
   StringReplace(stamp, " ", "T");
   return stamp + "Z";
}

string NextRequestId(string action)
{
   string counter_name =
      "FTMO.RG.RequestSequence." +
      StringFormat(
         "%I64d.%I64u",
         AccountInfoInteger(ACCOUNT_LOGIN),
         MagicNumber);
   double current = GlobalVariableCheck(counter_name)
      ? GlobalVariableGet(counter_name)
      : 0.0;
   double next = current + 1.0;
   if(GlobalVariableSet(counter_name, next) == 0)
   {
      next = (double)GetTickCount64();
   }
   return "mt5-" + action + "-" +
      IntegerToString((int)TimeGMT()) + "-" +
      StringFormat("%I64u", (ulong)next);
}

string UlongText(ulong value)
{
   return StringFormat("%I64u", value);
}

bool JsonTrue(string json, string field)
{
   return StringFind(json, "\"" + field + "\":true") >= 0;
}

bool PostJson(
   string path,
   string body,
   string request_id,
   string &response)
{
   char data[];
   char result[];
   string result_headers;
   string headers =
      "Content-Type: application/json\r\n"
      "X-Account-Credential: " + AccountCredential + "\r\n"
      "X-Request-Id: " + request_id + "\r\n";

   int copied = StringToCharArray(
      body,
      data,
      0,
      StringLen(body),
      CP_UTF8);
   if(copied <= 0)
   {
      Print("RiskGuard: unable to encode JSON body");
      return false;
   }

   ResetLastError();
   int status = WebRequest(
      "POST",
      RiskApiBaseUrl + path,
      headers,
      RiskApiTimeoutMs,
      data,
      result,
      result_headers);

   response = CharArrayToString(result, 0, -1, CP_UTF8);
   if(status != 200)
   {
      PrintFormat(
         "Risk API failure: path=%s status=%d error=%d response=%s",
         path,
         status,
         GetLastError(),
         response);
      return false;
   }
   return true;
}

double CurrentOpenRisk()
{
   double total = 0.0;

   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0 || !PositionSelectByTicket(ticket))
      {
         continue;
      }

      string symbol = PositionGetString(POSITION_SYMBOL);
      double volume = PositionGetDouble(POSITION_VOLUME);
      double stop_loss = PositionGetDouble(POSITION_SL);
      if(stop_loss <= 0.0)
      {
         return InitialCapital;
      }

      ENUM_POSITION_TYPE position_type =
         (ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE);
      ENUM_ORDER_TYPE order_type =
         position_type == POSITION_TYPE_BUY
         ? ORDER_TYPE_BUY
         : ORDER_TYPE_SELL;
      MqlTick tick;
      if(!SymbolInfoTick(symbol, tick))
      {
         return InitialCapital;
      }
      double current_price =
         position_type == POSITION_TYPE_BUY
         ? tick.bid
         : tick.ask;
      double profit = 0.0;

      if(!OrderCalcProfit(
            order_type,
            symbol,
            volume,
            current_price,
            stop_loss,
            profit))
      {
         return InitialCapital;
      }

      if(profit < 0.0)
      {
         total += MathAbs(profit);
      }
   }

   for(int i = OrdersTotal() - 1; i >= 0; i--)
   {
      ulong ticket = OrderGetTicket(i);
      if(ticket == 0 || !OrderSelect(ticket))
      {
         return InitialCapital;
      }

      ENUM_ORDER_TYPE pending_type =
         (ENUM_ORDER_TYPE)OrderGetInteger(ORDER_TYPE);
      ENUM_ORDER_TYPE calc_type = ORDER_TYPE_BUY;
      bool supported = true;
      if(pending_type == ORDER_TYPE_BUY_LIMIT ||
         pending_type == ORDER_TYPE_BUY_STOP ||
         pending_type == ORDER_TYPE_BUY_STOP_LIMIT)
      {
         calc_type = ORDER_TYPE_BUY;
      }
      else if(pending_type == ORDER_TYPE_SELL_LIMIT ||
         pending_type == ORDER_TYPE_SELL_STOP ||
         pending_type == ORDER_TYPE_SELL_STOP_LIMIT)
      {
         calc_type = ORDER_TYPE_SELL;
      }
      else
      {
         supported = false;
      }
      if(!supported)
      {
         continue;
      }

      string symbol = OrderGetString(ORDER_SYMBOL);
      double volume = OrderGetDouble(ORDER_VOLUME_CURRENT);
      double open_price = OrderGetDouble(ORDER_PRICE_OPEN);
      double stop_loss = OrderGetDouble(ORDER_SL);
      if(stop_loss <= 0.0)
      {
         return InitialCapital;
      }
      double profit = 0.0;
      if(!OrderCalcProfit(
            calc_type,
            symbol,
            volume,
            open_price,
            stop_loss,
            profit))
      {
         return InitialCapital;
      }
      if(profit < 0.0)
      {
         total += MathAbs(profit);
      }
   }
   return total;
}

bool SyncAccount()
{
   string payload =
      "{"
      "\"account_id\":" + JsonQuote(AccountId) + ","
      "\"account_type\":" + JsonQuote(AccountType) + ","
      "\"phase\":" + JsonQuote(AccountPhase) + ","
      "\"style\":" + JsonQuote(AccountStyle) + ","
      "\"initial_capital\":\"" +
         DoubleToString(InitialCapital, 2) + "\","
      "\"day_start_balance\":\"" +
         DoubleToString(BootstrapDayStartBalance, 2) + "\","
      "\"highest_settled_balance\":\"" +
         DoubleToString(BootstrapHighestSettledBalance, 2) + "\","
      "\"balance\":\"" +
         DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE), 2) + "\","
      "\"equity\":\"" +
         DoubleToString(AccountInfoDouble(ACCOUNT_EQUITY), 2) + "\","
      "\"current_open_risk\":\"" +
         DoubleToString(CurrentOpenRisk(), 2) + "\","
      "\"as_of\":" + JsonQuote(IsoUtc(TimeGMT()))
      + "}";

   string response;
   return PostJson(
      "/v1/account-sync",
      payload,
      NextRequestId("sync"),
      response);
}

bool LossPerLot(
   string symbol,
   ENUM_ORDER_TYPE order_type,
   double entry_price,
   double stop_loss,
   double &loss_per_lot)
{
   double profit = 0.0;
   if(!OrderCalcProfit(
         order_type,
         symbol,
         1.0,
         entry_price,
         stop_loss,
         profit))
   {
      return false;
   }
   loss_per_lot = MathAbs(profit);
   return loss_per_lot > 0.0;
}

double NormalizeVolumeForSymbol(string symbol, double requested_volume)
{
   double min_volume = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
   double max_volume = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MAX);
   double step = SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP);
   if(min_volume <= 0.0 || max_volume <= 0.0 || step <= 0.0)
   {
      return 0.0;
   }
   if(requested_volume < min_volume || requested_volume > max_volume)
   {
      return 0.0;
   }
   double normalized = MathFloor(requested_volume / step + 1e-9) * step;
   normalized = NormalizeDouble(normalized, 8);
   if(normalized < min_volume || normalized > max_volume)
   {
      return 0.0;
   }
   return normalized;
}

bool EvaluateRequest(
   string request_json,
   string request_id,
   string &response)
{
   if(!SyncAccount())
   {
      Print("RiskGuard: account sync failed; risk increase blocked");
      return false;
   }

   string payload =
      "{"
      "\"account_id\":" + JsonQuote(AccountId) + ","
      "\"request\":" + request_json
      + "}";
   if(!PostJson("/v1/evaluate", payload, request_id, response))
   {
      return false;
   }
   return JsonTrue(response, "allowed");
}

void ReportExecution(
   string request_id,
   string action,
   string symbol,
   string outcome,
   string platform_status,
   string platform_order_id)
{
   if(outcome == "unknown")
   {
      UnknownExecutionLock = true;
      GlobalVariableSet(UnknownLockName(), 1.0);
   }
   string payload =
      "{"
      "\"account_id\":" + JsonQuote(AccountId) + ","
      "\"request_id\":" + JsonQuote(request_id) + ","
      "\"outcome\":" + JsonQuote(outcome) + ","
      "\"action\":" + JsonQuote(action) + ","
      "\"symbol\":" + JsonQuote(symbol) + ","
      "\"occurred_at\":" + JsonQuote(IsoUtc(TimeGMT())) + ","
      "\"platform_status\":" + JsonQuote(platform_status) + ","
      "\"platform_order_id\":" + JsonQuote(platform_order_id)
      + "}";
   string response;
   if(!PostJson(
         "/v1/execution-result",
         payload,
         NextRequestId("execution"),
         response))
   {
      PrintFormat(
         "RiskGuard: execution audit unresolved request_id=%s",
         request_id);
   }
}

bool TradeRetcodeSuccessful(uint retcode)
{
   return retcode == TRADE_RETCODE_DONE ||
      retcode == TRADE_RETCODE_DONE_PARTIAL;
}

bool TradeRetcodeDefinitelyFailed(uint retcode)
{
   return retcode == TRADE_RETCODE_REQUOTE ||
      retcode == TRADE_RETCODE_REJECT ||
      retcode == TRADE_RETCODE_CANCEL ||
      retcode == TRADE_RETCODE_INVALID ||
      retcode == TRADE_RETCODE_INVALID_VOLUME ||
      retcode == TRADE_RETCODE_INVALID_PRICE ||
      retcode == TRADE_RETCODE_INVALID_STOPS ||
      retcode == TRADE_RETCODE_TRADE_DISABLED ||
      retcode == TRADE_RETCODE_MARKET_CLOSED ||
      retcode == TRADE_RETCODE_NO_MONEY ||
      retcode == TRADE_RETCODE_PRICE_CHANGED ||
      retcode == TRADE_RETCODE_PRICE_OFF ||
      retcode == TRADE_RETCODE_INVALID_EXPIRATION ||
      retcode == TRADE_RETCODE_TOO_MANY_REQUESTS ||
      retcode == TRADE_RETCODE_NO_CHANGES ||
      retcode == TRADE_RETCODE_SERVER_DISABLES_AT ||
      retcode == TRADE_RETCODE_CLIENT_DISABLES_AT ||
      retcode == TRADE_RETCODE_LOCKED ||
      retcode == TRADE_RETCODE_FROZEN ||
      retcode == TRADE_RETCODE_LIMIT_ORDERS ||
      retcode == TRADE_RETCODE_LIMIT_VOLUME ||
      retcode == TRADE_RETCODE_INVALID_ORDER ||
      retcode == TRADE_RETCODE_INVALID_FILL ||
      retcode == TRADE_RETCODE_INVALID_CLOSE_VOLUME ||
      retcode == TRADE_RETCODE_LIMIT_POSITIONS ||
      retcode == TRADE_RETCODE_REJECT_CANCEL ||
      retcode == TRADE_RETCODE_LONG_ONLY ||
      retcode == TRADE_RETCODE_SHORT_ONLY ||
      retcode == TRADE_RETCODE_CLOSE_ONLY ||
      retcode == TRADE_RETCODE_FIFO_CLOSE ||
      retcode == TRADE_RETCODE_HEDGE_PROHIBITED;
}

string TradeOutcome(bool submitted, uint retcode)
{
   if(submitted && TradeRetcodeSuccessful(retcode))
   {
      return "success";
   }
   if(TradeRetcodeDefinitelyFailed(retcode))
   {
      return "failure";
   }
   return "unknown";
}

bool RiskGuardMarket(
   string symbol,
   ENUM_ORDER_TYPE order_type,
   double volume,
   double stop_loss,
   double take_profit,
   string idea_id)
{
   if(UnknownExecutionLock)
   {
      Print("RiskGuard: new risk blocked by unknown execution lock");
      return false;
   }
   volume = NormalizeVolumeForSymbol(symbol, volume);
   if(volume <= 0.0 || stop_loss <= 0.0)
   {
      Print("RiskGuard: volume and stop loss must be positive");
      return false;
   }

   MqlTick tick;
   if(!SymbolInfoTick(symbol, tick))
   {
      Print("RiskGuard: unable to read current tick");
      return false;
   }

   bool is_buy = order_type == ORDER_TYPE_BUY;
   double entry_price = is_buy ? tick.ask : tick.bid;
   if((is_buy && stop_loss >= entry_price) ||
      (!is_buy && stop_loss <= entry_price))
   {
      Print("RiskGuard: stop loss is on the wrong side of entry");
      return false;
   }

   double loss_per_lot = 0.0;
   if(!LossPerLot(
         symbol,
         order_type,
         entry_price,
         stop_loss,
         loss_per_lot))
   {
      Print("RiskGuard: unable to calculate loss per lot");
      return false;
   }

   string request_id = NextRequestId("open");
   string request =
      "{"
      "\"symbol\":" + JsonQuote(symbol) + ","
      "\"action\":\"open\","
      "\"requested_at\":" + JsonQuote(IsoUtc(TimeGMT())) + ","
      "\"volume\":\"" + DoubleToString(volume, 8) + "\","
      "\"entry_price\":\"" + DoubleToString(entry_price, 8) + "\","
      "\"stop_loss\":\"" + DoubleToString(stop_loss, 8) + "\","
      "\"loss_per_volume_unit\":\"" +
         DoubleToString(loss_per_lot, 8) + "\","
      "\"estimated_costs\":\"" +
         DoubleToString(EstimatedCostsPerTrade, 2) + "\","
      "\"is_risk_increasing\":true,"
      "\"idea_id\":" + JsonQuote(idea_id)
      + "}";

   string response;
   if(!EvaluateRequest(request, request_id, response))
   {
      PrintFormat("RiskGuard rejected open: %s", response);
      return false;
   }

   Trade.SetExpertMagicNumber(MagicNumber);
   bool submitted = is_buy
      ? Trade.Buy(
         volume,
         symbol,
         0.0,
         stop_loss,
         take_profit,
         idea_id)
      : Trade.Sell(
         volume,
         symbol,
         0.0,
         stop_loss,
         take_profit,
         idea_id);
   uint retcode = Trade.ResultRetcode();
   bool success = submitted && TradeRetcodeSuccessful(retcode);

   ReportExecution(
      request_id,
      "open",
      symbol,
      TradeOutcome(submitted, retcode),
      IntegerToString((int)retcode),
      UlongText(Trade.ResultOrder()));

   if(!success)
   {
      PrintFormat(
         "RiskGuard platform rejection: request_id=%s retcode=%u",
         request_id,
         retcode);
   }
   return success;
}

bool RiskGuardBuy(
   string symbol,
   double volume,
   double stop_loss,
   double take_profit,
   string idea_id)
{
   return RiskGuardMarket(
      symbol,
      ORDER_TYPE_BUY,
      volume,
      stop_loss,
      take_profit,
      idea_id);
}

bool RiskGuardSell(
   string symbol,
   double volume,
   double stop_loss,
   double take_profit,
   string idea_id)
{
   return RiskGuardMarket(
      symbol,
      ORDER_TYPE_SELL,
      volume,
      stop_loss,
      take_profit,
      idea_id);
}

bool RiskGuardClose(ulong position_ticket)
{
   if(!PositionSelectByTicket(position_ticket))
   {
      return false;
   }
   string symbol = PositionGetString(POSITION_SYMBOL);
   string request_id = NextRequestId("close");
   string request =
      "{"
      "\"symbol\":" + JsonQuote(symbol) + ","
      "\"action\":\"close\","
      "\"requested_at\":" + JsonQuote(IsoUtc(TimeGMT())) + ","
      "\"is_risk_increasing\":false"
      + "}";

   string response;
   if(!EvaluateRequest(request, request_id, response))
   {
      PrintFormat("RiskGuard rejected close: %s", response);
      return false;
   }

   bool submitted = Trade.PositionClose(position_ticket);
   uint retcode = Trade.ResultRetcode();
   bool success = submitted && TradeRetcodeSuccessful(retcode);
   ReportExecution(
      request_id,
      "close",
      symbol,
      TradeOutcome(submitted, retcode),
      IntegerToString((int)retcode),
      UlongText(Trade.ResultOrder()));
   return success;
}

bool RiskGuardClosePartial(ulong position_ticket, double volume)
{
   if(volume <= 0.0 || !PositionSelectByTicket(position_ticket))
   {
      return false;
   }
   string symbol = PositionGetString(POSITION_SYMBOL);
   double position_volume = PositionGetDouble(POSITION_VOLUME);
   volume = MathMin(volume, position_volume);
   volume = NormalizeVolumeForSymbol(symbol, volume);
   if(volume <= 0.0)
   {
      return false;
   }
   string request_id = NextRequestId("close-partial");
   string request =
      "{"
      "\"symbol\":" + JsonQuote(symbol) + ","
      "\"action\":\"close\","
      "\"requested_at\":" + JsonQuote(IsoUtc(TimeGMT())) + ","
      "\"volume\":\"" + DoubleToString(volume, 8) + "\","
      "\"is_risk_increasing\":false"
      + "}";

   string response;
   if(!EvaluateRequest(request, request_id, response))
   {
      PrintFormat("RiskGuard rejected partial close: %s", response);
      return false;
   }

   bool submitted = Trade.PositionClosePartial(position_ticket, volume);
   uint retcode = Trade.ResultRetcode();
   bool success = submitted && TradeRetcodeSuccessful(retcode);
   ReportExecution(
      request_id,
      "close",
      symbol,
      TradeOutcome(submitted, retcode),
      IntegerToString((int)retcode),
      UlongText(Trade.ResultOrder()));
   return success;
}

bool RiskGuardModifyPosition(
   ulong position_ticket,
   double stop_loss,
   double take_profit,
   bool is_risk_increasing)
{
   if(!PositionSelectByTicket(position_ticket))
   {
      return false;
   }
   string symbol = PositionGetString(POSITION_SYMBOL);
   if(is_risk_increasing && stop_loss <= 0.0)
   {
      Print("RiskGuard: risk-increasing modification requires a stop loss");
      return false;
   }
   double volume = PositionGetDouble(POSITION_VOLUME);
   double entry_price = PositionGetDouble(POSITION_PRICE_OPEN);
   double current_stop = PositionGetDouble(POSITION_SL);
   ENUM_POSITION_TYPE position_type =
      (ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE);
   ENUM_ORDER_TYPE order_type =
      position_type == POSITION_TYPE_BUY
      ? ORDER_TYPE_BUY
      : ORDER_TYPE_SELL;
   if(stop_loss > 0.0 &&
      ((position_type == POSITION_TYPE_BUY && stop_loss >= entry_price) ||
       (position_type == POSITION_TYPE_SELL && stop_loss <= entry_price)))
   {
      Print("RiskGuard: modified stop loss is on the wrong side");
      return false;
   }
   double current_risk = InitialCapital;
   double new_risk = InitialCapital;
   double profit = 0.0;
   if(current_stop > 0.0 &&
      OrderCalcProfit(
         order_type,
         symbol,
         volume,
         entry_price,
         current_stop,
         profit))
   {
      current_risk = profit < 0.0 ? MathAbs(profit) : 0.0;
   }
   if(stop_loss > 0.0 &&
      OrderCalcProfit(
         order_type,
         symbol,
         volume,
         entry_price,
         stop_loss,
         profit))
   {
      new_risk = profit < 0.0 ? MathAbs(profit) : 0.0;
   }
   double additional_risk = MathMax(0.0, new_risk - current_risk);
   bool effective_risk_increasing = additional_risk > 0.01;
   if(effective_risk_increasing && UnknownExecutionLock)
   {
      Print(
         "RiskGuard: risk-increasing modification blocked by "
         "unknown execution lock");
      return false;
   }
   string request_id = NextRequestId("modify");
   string request =
      "{"
      "\"symbol\":" + JsonQuote(symbol) + ","
      "\"action\":\"modify\","
      "\"requested_at\":" + JsonQuote(IsoUtc(TimeGMT())) + ","
      "\"stop_loss\":\"" + DoubleToString(stop_loss, 8) + "\","
      "\"additional_risk\":\"" +
         DoubleToString(additional_risk, 2) + "\","
      "\"is_risk_increasing\":" +
         (effective_risk_increasing ? "true" : "false")
      + "}";

   string response;
   if(!EvaluateRequest(request, request_id, response))
   {
      PrintFormat("RiskGuard rejected position modification: %s", response);
      return false;
   }

   bool submitted = Trade.PositionModify(
      position_ticket,
      stop_loss,
      take_profit);
   uint retcode = Trade.ResultRetcode();
   bool success = submitted && TradeRetcodeSuccessful(retcode);
   ReportExecution(
      request_id,
      "modify",
      symbol,
      TradeOutcome(submitted, retcode),
      IntegerToString((int)retcode),
      UlongText(Trade.ResultOrder()));
   return success;
}

bool RiskGuardCancelPending(ulong order_ticket)
{
   if(!OrderSelect(order_ticket))
   {
      return false;
   }
   string symbol = OrderGetString(ORDER_SYMBOL);
   string request_id = NextRequestId("cancel");
   string request =
      "{"
      "\"symbol\":" + JsonQuote(symbol) + ","
      "\"action\":\"cancel\","
      "\"requested_at\":" + JsonQuote(IsoUtc(TimeGMT())) + ","
      "\"is_risk_increasing\":false"
      + "}";

   string response;
   if(!EvaluateRequest(request, request_id, response))
   {
      PrintFormat("RiskGuard rejected pending cancellation: %s", response);
      return false;
   }

   bool submitted = Trade.OrderDelete(order_ticket);
   uint retcode = Trade.ResultRetcode();
   bool success = submitted && TradeRetcodeSuccessful(retcode);
   ReportExecution(
      request_id,
      "cancel",
      symbol,
      TradeOutcome(submitted, retcode),
      IntegerToString((int)retcode),
      UlongText(Trade.ResultOrder()));
   return success;
}

bool GuardStatus(string path, string symbol, string &response)
{
   string payload =
      "{"
      "\"account_id\":" + JsonQuote(AccountId) + ","
      "\"symbol\":" + JsonQuote(symbol) + ","
      "\"now\":" + JsonQuote(IsoUtc(TimeGMT()))
      + "}";
   return PostJson(
      path,
      payload,
      NextRequestId("news"),
      response);
}

void RunNewsGuard()
{
   if(!SyncAccount())
   {
      Print("RiskGuard: news guard cannot sync account");
      return;
   }

   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0 || !PositionSelectByTicket(ticket))
      {
         continue;
      }
      string symbol = PositionGetString(POSITION_SYMBOL);
      string news_response;
      string market_response;
      bool news_ok =
         GuardStatus("/v1/news-status", symbol, news_response);
      bool market_ok =
         GuardStatus("/v1/market-status", symbol, market_response);
      bool force_flat =
         (news_ok && JsonTrue(news_response, "force_flat")) ||
         (market_ok && JsonTrue(market_response, "force_flat"));
      if(force_flat)
      {
         RiskGuardClose(ticket);
      }
      else if(!news_ok || !market_ok ||
         (news_ok && JsonTrue(news_response, "emergency_alert")) ||
         (market_ok && JsonTrue(market_response, "emergency_alert")))
      {
         Alert(
            "FTMO RiskGuard emergency: position status is unresolved or "
            "restricted: ",
            symbol);
      }
   }

   for(int i = OrdersTotal() - 1; i >= 0; i--)
   {
      ulong ticket = OrderGetTicket(i);
      if(ticket == 0 || !OrderSelect(ticket))
      {
         continue;
      }
      string symbol = OrderGetString(ORDER_SYMBOL);
      string news_response;
      string market_response;
      bool news_ok =
         GuardStatus("/v1/news-status", symbol, news_response);
      bool market_ok =
         GuardStatus("/v1/market-status", symbol, market_response);
      bool cancel_pending =
         (news_ok && JsonTrue(news_response, "cancel_pending")) ||
         (market_ok && JsonTrue(market_response, "cancel_pending"));
      if(cancel_pending)
      {
         if(!RiskGuardCancelPending(ticket))
         {
            PrintFormat(
               "RiskGuard: unable to cancel pending order %I64u",
               ticket);
         }
      }
      else if(!news_ok || !market_ok)
      {
         PrintFormat(
            "RiskGuard: pending order status unresolved ticket=%I64u",
            ticket);
      }
   }
}

int OnInit()
{
   if(StringLen(AccountId) == 0)
   {
      Print("RiskGuard: AccountId is required");
      return INIT_PARAMETERS_INCORRECT;
   }
   if(StringLen(AccountCredential) == 0)
   {
      Print("RiskGuard: AccountCredential is required");
      return INIT_PARAMETERS_INCORRECT;
   }
   LoadUnknownExecutionLock();
   EventSetTimer(MathMax(1, NewsGuardIntervalSeconds));
   if(!SyncAccount())
   {
      Print(
         "RiskGuard initialized fail-closed; verify WebRequest whitelist "
         "and local API");
   }
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   EventKillTimer();
}

void OnTimer()
{
   RunNewsGuard();
}

void OnTick()
{
   // Strategy code calls RiskGuardBuy(), RiskGuardSell(), or RiskGuardClose().
}
