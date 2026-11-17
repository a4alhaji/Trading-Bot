#property strict
#property version   "1.10"
#property description "Local bridge between MT5 for Mac and the Python trading dashboard"

#include <Trade/Trade.mqh>

input string BridgeUrl = "http://127.0.0.1:8766";   // must also be in Tools > Options > Expert Advisors > WebRequest list
input string BridgeToken = "bda1e832f1874f8d90e60e148facdc9a92756d3a6eeb4f1b";
input string TradeSymbol = "XAUUSD247m";
input ENUM_TIMEFRAMES SignalTimeframe = PERIOD_M15;
input ulong TradeMagic = 560011;
input int HistoryDays = 30;
input bool AllowLiveTrading = false;

CTrade trade;
string last_command_id = "";
string last_command_result = "";

string EscapeJson(const string value)
{
   string out = "";
   for(int i = 0; i < StringLen(value); i++)
   {
      ushort c = StringGetCharacter(value, i);
      if(c == 34) out += "\\\"";
      else if(c == 92) out += "\\\\";
      else if(c == 10) out += "\\n";
      else if(c == 13) out += "\\r";
      else if(c == 9) out += "\\t";
      else if(c >= 32) out += ShortToString(c);
   }
   return out;
}

string Number(const double value)
{
   return DoubleToString(value, 8);
}

string GetString(const string json, const string key)
{
   string needle = "\"" + key + "\":\"";
   int start = StringFind(json, needle);
   if(start < 0) return "";
   start += StringLen(needle);
   int end = StringFind(json, "\"", start);
   if(end < 0) return "";
   return StringSubstr(json, start, end - start);
}

double GetNumber(const string json, const string key)
{
   string needle = "\"" + key + "\":";
   int start = StringFind(json, needle);
   if(start < 0) return 0;
   start += StringLen(needle);
   int end = start;
   while(end < StringLen(json))
   {
      ushort c = StringGetCharacter(json, end);
      if(c == 44 || c == 125 || c == 32) break;
      end++;
   }
   return StringToDouble(StringSubstr(json, start, end - start));
}

string last_failure = "";
datetime last_failure_time = 0;
bool bridge_ok = false;

// Explains a failed request in the Experts tab (repeats at most every 30 s).
void ReportFailure(const string what, const int status, const int err, char &body[])
{
   string why;
   if(status == -1 && err == 4014)
      why = "URL not allowed. Add " + BridgeUrl + " to Tools > Options > Expert Advisors > "
            + "'Allow WebRequest for listed URL' (tick the box), then re-attach the EA.";
   else if(status == -1 && (err == 5200 || err == 5201 || err == 5202 || err == 5203))
      why = "Cannot reach Python at " + BridgeUrl + " (error " + IntegerToString(err)
            + "). Is 'python bot.py' running? Is the address/port right?";
   else if(status == -1)
      why = "WebRequest error " + IntegerToString(err);
   else
      why = "HTTP " + IntegerToString(status) + ": " + CharArrayToString(body, 0, -1, CP_UTF8);

   string msg = what + " -> " + why;
   if(msg != last_failure || TimeLocal() - last_failure_time >= 30)
   {
      Print("Bridge failed: ", msg);
      last_failure = msg;
      last_failure_time = TimeLocal();
   }
   bridge_ok = false;
}

void ReportSuccess()
{
   if(!bridge_ok)
      Print("Bridge connected to Python at ", BridgeUrl);
   bridge_ok = true;
   last_failure = "";
}

bool PostJson(const string path, const string body, string &response)
{
   char data[], result[];
   string result_headers;
   int length = StringToCharArray(body, data, 0, WHOLE_ARRAY, CP_UTF8);
   if(length > 0) ArrayResize(data, length - 1);
   string headers = "Content-Type: application/json\r\nX-Bridge-Token: " + BridgeToken + "\r\n";
   ResetLastError();
   int status = WebRequest("POST", BridgeUrl + path, headers, 5000, data, result, result_headers);
   if(status < 200 || status >= 300)
   {
      ReportFailure("POST " + path, status, GetLastError(), result);
      return false;
   }
   ReportSuccess();
   response = CharArrayToString(result, 0, -1, CP_UTF8);
   return true;
}

string BuildSnapshot()
{
   MqlTick tick;
   if(!SymbolInfoTick(TradeSymbol, tick))
      return "{\"connected\":false,\"error\":\"No quote for configured symbol\"}";

   long account_mode = AccountInfoInteger(ACCOUNT_TRADE_MODE);
   bool demo = (account_mode == ACCOUNT_TRADE_MODE_DEMO);
   bool terminal_connected = (bool)TerminalInfoInteger(TERMINAL_CONNECTED);
   bool algo_allowed = (bool)TerminalInfoInteger(TERMINAL_TRADE_ALLOWED)
                       && (bool)MQLInfoInteger(MQL_TRADE_ALLOWED);
   long symbol_mode = SymbolInfoInteger(TradeSymbol, SYMBOL_TRADE_MODE);
   int digits = (int)SymbolInfoInteger(TradeSymbol, SYMBOL_DIGITS);

   string json = "{\"connected\":" + (terminal_connected ? "true" : "false")
      + ",\"symbol\":\"" + EscapeJson(TradeSymbol) + "\",\"timeframe\":\""
      + EnumToString(SignalTimeframe) + "\",\"account\":{\"login\":"
      + IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN)) + ",\"server\":\""
      + EscapeJson(AccountInfoString(ACCOUNT_SERVER)) + "\",\"currency\":\""
      + EscapeJson(AccountInfoString(ACCOUNT_CURRENCY)) + "\",\"balance\":"
      + Number(AccountInfoDouble(ACCOUNT_BALANCE)) + ",\"equity\":"
      + Number(AccountInfoDouble(ACCOUNT_EQUITY)) + ",\"margin\":"
      + Number(AccountInfoDouble(ACCOUNT_MARGIN)) + ",\"free_margin\":"
      + Number(AccountInfoDouble(ACCOUNT_MARGIN_FREE)) + ",\"margin_level\":"
      + Number(AccountInfoDouble(ACCOUNT_MARGIN_LEVEL)) + ",\"leverage\":"
      + IntegerToString(AccountInfoInteger(ACCOUNT_LEVERAGE)) + ",\"demo\":"
      + (demo ? "true" : "false") + "},\"terminal\":{\"connected\":"
      + (terminal_connected ? "true" : "false") + ",\"algo_trading\":"
      + (algo_allowed ? "true" : "false") + "},\"tick\":{\"bid\":"
      + Number(tick.bid) + ",\"ask\":" + Number(tick.ask) + ",\"time\":"
      + IntegerToString((long)tick.time) + "},\"symbol_info\":{\"digits\":"
      + IntegerToString(digits) + ",\"point\":" + Number(SymbolInfoDouble(TradeSymbol, SYMBOL_POINT))
      + ",\"stops_level\":" + IntegerToString(SymbolInfoInteger(TradeSymbol, SYMBOL_TRADE_STOPS_LEVEL))
      + ",\"trade_mode\":\"" + (symbol_mode == SYMBOL_TRADE_MODE_FULL ? "full" : "closed")
      + "\",\"volume_min\":" + Number(SymbolInfoDouble(TradeSymbol, SYMBOL_VOLUME_MIN))
      + ",\"volume_max\":" + Number(SymbolInfoDouble(TradeSymbol, SYMBOL_VOLUME_MAX))
      + ",\"volume_step\":" + Number(SymbolInfoDouble(TradeSymbol, SYMBOL_VOLUME_STEP))
      + ",\"filling_mode\":" + IntegerToString(SymbolInfoInteger(TradeSymbol, SYMBOL_FILLING_MODE))
      + "},\"rates\":[";

   MqlRates rates[];
   ArraySetAsSeries(rates, true);
   int copied = CopyRates(TradeSymbol, SignalTimeframe, 0, 500, rates);
   bool first = true;
   for(int i = copied - 1; i >= 0; i--)
   {
      if(!first) json += ",";
      first = false;
      json += "{\"time\":" + IntegerToString((long)rates[i].time)
         + ",\"open\":" + Number(rates[i].open) + ",\"high\":" + Number(rates[i].high)
         + ",\"low\":" + Number(rates[i].low) + ",\"close\":" + Number(rates[i].close) + "}";
   }

   json += "],\"positions\":[";
   first = true;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0 || PositionGetString(POSITION_SYMBOL) != TradeSymbol
         || (ulong)PositionGetInteger(POSITION_MAGIC) != TradeMagic)
         continue;
      if(!first) json += ",";
      first = false;
      json += "{\"ticket\":" + IntegerToString((long)ticket)
         + ",\"magic\":" + IntegerToString((long)TradeMagic)
         + ",\"symbol\":\"" + EscapeJson(TradeSymbol) + "\",\"side\":\""
         + (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY ? "BUY" : "SELL")
         + "\",\"volume\":" + Number(PositionGetDouble(POSITION_VOLUME))
         + ",\"open_price\":" + Number(PositionGetDouble(POSITION_PRICE_OPEN))
         + ",\"price\":" + Number(PositionGetDouble(POSITION_PRICE_CURRENT))
         + ",\"sl\":" + Number(PositionGetDouble(POSITION_SL))
         + ",\"tp\":" + Number(PositionGetDouble(POSITION_TP))
         + ",\"profit\":" + Number(PositionGetDouble(POSITION_PROFIT))
         + ",\"swap\":" + Number(PositionGetDouble(POSITION_SWAP))
         + ",\"time\":" + IntegerToString((long)PositionGetInteger(POSITION_TIME)) + "}";
   }

   json += "],\"deals\":[";
   first = true;
   datetime from = TimeCurrent() - (datetime)(HistoryDays * 86400);
   if(HistorySelect(from, TimeCurrent()))
   {
      for(int i = 0; i < HistoryDealsTotal(); i++)
      {
         ulong ticket = HistoryDealGetTicket(i);
         if(ticket == 0 || (ulong)HistoryDealGetInteger(ticket, DEAL_MAGIC) != TradeMagic
            || HistoryDealGetString(ticket, DEAL_SYMBOL) != TradeSymbol)
            continue;
         if(!first) json += ",";
         first = false;
         json += "{\"ticket\":" + IntegerToString((long)ticket)
            + ",\"time\":" + IntegerToString((long)HistoryDealGetInteger(ticket, DEAL_TIME))
            + ",\"magic\":" + IntegerToString((long)TradeMagic)
            + ",\"symbol\":\"" + EscapeJson(TradeSymbol) + "\",\"entry\":"
            + IntegerToString(HistoryDealGetInteger(ticket, DEAL_ENTRY))
            + ",\"type\":" + IntegerToString(HistoryDealGetInteger(ticket, DEAL_TYPE))
            + ",\"reason\":" + IntegerToString(HistoryDealGetInteger(ticket, DEAL_REASON))
            + ",\"volume\":" + Number(HistoryDealGetDouble(ticket, DEAL_VOLUME))
            + ",\"price\":" + Number(HistoryDealGetDouble(ticket, DEAL_PRICE))
            + ",\"profit\":" + Number(HistoryDealGetDouble(ticket, DEAL_PROFIT))
            + ",\"commission\":" + Number(HistoryDealGetDouble(ticket, DEAL_COMMISSION))
            + ",\"swap\":" + Number(HistoryDealGetDouble(ticket, DEAL_SWAP)) + "}";
      }
   }
   return json + "]}";
}

string ExecuteCommand(const string command)
{
   string id = GetString(command, "id");
   string action = GetString(command, "action");
   string result_data = "{}";
   bool ok = false;
   string error = "";

   if(action == "calc_profit")
   {
      ENUM_ORDER_TYPE type = GetString(command, "side") == "BUY" ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
      double profit = 0;
      if(OrderCalcProfit(type, GetString(command, "symbol"), GetNumber(command, "volume"),
                         GetNumber(command, "entry"), GetNumber(command, "exit"), profit))
      {
         ok = true;
         result_data = "{\"value\":" + Number(profit) + "}";
      }
      else error = "OrderCalcProfit failed: " + IntegerToString(GetLastError());
   }
   else if(action == "order")
   {
      if(!AllowLiveTrading && AccountInfoInteger(ACCOUNT_TRADE_MODE) != ACCOUNT_TRADE_MODE_DEMO)
         error = "EA live-trading safeguard is off";
      else
      {
         trade.SetExpertMagicNumber(TradeMagic);
         trade.SetDeviationInPoints((ulong)GetNumber(command, "deviation"));
         string side = GetString(command, "side");
         bool sent = side == "BUY"
            ? trade.Buy(GetNumber(command, "volume"), TradeSymbol, 0,
                        GetNumber(command, "sl"), GetNumber(command, "tp"), "xauusd-bot")
            : trade.Sell(GetNumber(command, "volume"), TradeSymbol, 0,
                         GetNumber(command, "sl"), GetNumber(command, "tp"), "xauusd-bot");
         uint retcode = trade.ResultRetcode();
         ok = sent && (retcode == TRADE_RETCODE_DONE || retcode == TRADE_RETCODE_PLACED
                       || retcode == TRADE_RETCODE_DONE_PARTIAL);
         if(ok)
            result_data = "{\"retcode\":" + IntegerToString((int)retcode)
               + ",\"comment\":\"" + EscapeJson(trade.ResultRetcodeDescription()) + "\"}";
         else error = "Order failed: " + IntegerToString((int)retcode) + " "
                      + trade.ResultRetcodeDescription();
      }
   }
   else if(action == "close")
   {
      ulong ticket = (ulong)GetNumber(command, "ticket");
      if(PositionSelectByTicket(ticket)
         && PositionGetString(POSITION_SYMBOL) == TradeSymbol
         && (ulong)PositionGetInteger(POSITION_MAGIC) == TradeMagic)
      {
         trade.SetExpertMagicNumber(TradeMagic);
         bool sent = trade.PositionClose(ticket);
         uint retcode = trade.ResultRetcode();
         ok = sent && (retcode == TRADE_RETCODE_DONE || retcode == TRADE_RETCODE_DONE_PARTIAL);
         if(ok)
            result_data = "{\"retcode\":" + IntegerToString((int)retcode)
               + ",\"comment\":\"" + EscapeJson(trade.ResultRetcodeDescription()) + "\"}";
         else error = "Close failed: " + IntegerToString((int)retcode) + " "
                      + trade.ResultRetcodeDescription();
      }
      else error = "Position is not an open position belonging to this bot";
   }
   else error = "Unknown bridge command";

   return "{\"id\":\"" + EscapeJson(id) + "\",\"ok\":" + (ok ? "true" : "false")
      + ",\"error\":\"" + EscapeJson(error) + "\",\"result\":" + result_data + "}";
}

void PollCommand()
{
   string command;
   if(!PostJson("/bridge/commands", "{}", command)) return;
   string id = GetString(command, "id");
   if(id == "") return;
   if(id == last_command_id)
   {
      string ignored;
      PostJson("/bridge/result", last_command_result, ignored);
      return;
   }
   last_command_id = id;
   last_command_result = ExecuteCommand(command);
   string ignored;
   PostJson("/bridge/result", last_command_result, ignored);
}

int OnInit()
{
   if(!SymbolSelect(TradeSymbol, true))
   {
      Print("Could not select symbol: ", TradeSymbol);
      return INIT_FAILED;
   }
   EventSetTimer(1);
   Print("MT5 macOS bridge started for ", TradeSymbol, " ", EnumToString(SignalTimeframe),
         ". Add BridgeUrl to Tools > Options > Expert Advisors > WebRequest allow list.");
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   EventKillTimer();
}

void OnTimer()
{
   string response;
   PostJson("/bridge/snapshot", BuildSnapshot(), response);
   PollCommand();
}

void OnTick()
{
}
