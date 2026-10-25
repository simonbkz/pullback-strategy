//+------------------------------------------------------------------+
//|                                       ExportHistoryToCSV.mq5     |
//|  Run as a Script (not Expert) in MetaEditor: F4 to open editor,  |
//|  drag onto a chart of any symbol, set inputs, press OK.          |
//|                                                                  |
//|  Writes <symbol>_<TF>.csv into MQL5\Files\ — copy those into     |
//|  the fx-edge\data\ folder (replacing parquet if you want HFM-    |
//|  exact bars instead of yfinance).                                |
//+------------------------------------------------------------------+
#property copyright "fx-edge HFM exporter"
#property version   "1.00"
#property script_show_inputs

input string  Symbols     = "EURUSD,XAUUSD,US500,DE40,JP225,CHFJPY,HK50,US100,ETHUSD,USOIL"; // comma list
input ENUM_TIMEFRAMES TF1 = PERIOD_H1;
input ENUM_TIMEFRAMES TF2 = PERIOD_H4;
input int     YearsBack   = 5;

void OnStart()
{
    string syms[];
    int n = StringSplit(Symbols, ',', syms);
    datetime fromTime = TimeCurrent() - (datetime)(YearsBack * 365 * 24 * 60 * 60);

    for(int i = 0; i < n; i++)
    {
        string s = syms[i];
        StringTrimLeft(s); StringTrimRight(s);
        if(s == "") continue;
        ExportOne(s, TF1, fromTime);
        ExportOne(s, TF2, fromTime);
    }
    Print("Done. CSVs in: ", TerminalInfoString(TERMINAL_DATA_PATH), "\\MQL5\\Files\\");
}

void ExportOne(string symbol, ENUM_TIMEFRAMES tf, datetime fromTime)
{
    MqlRates rates[];
    int copied = CopyRates(symbol, tf, fromTime, TimeCurrent(), rates);
    if(copied <= 0)
    {
        PrintFormat("[skip] %s %s: no data", symbol, EnumToString(tf));
        return;
    }
    string fname = StringFormat("%s_%s.csv", symbol, PeriodSuffix(tf));
    int h = FileOpen(fname, FILE_WRITE | FILE_CSV | FILE_ANSI, ',');
    if(h == INVALID_HANDLE)
    {
        PrintFormat("[err] open %s err=%d", fname, GetLastError());
        return;
    }
    FileWrite(h, "ts", "open", "high", "low", "close", "tick_volume", "spread");
    for(int i = 0; i < copied; i++)
    {
        FileWrite(h,
                  TimeToString(rates[i].time, TIME_DATE | TIME_SECONDS),
                  DoubleToString(rates[i].open,  _Digits),
                  DoubleToString(rates[i].high,  _Digits),
                  DoubleToString(rates[i].low,   _Digits),
                  DoubleToString(rates[i].close, _Digits),
                  rates[i].tick_volume,
                  rates[i].spread);
    }
    FileClose(h);
    PrintFormat("[ok] %s %s: %d bars -> %s", symbol, EnumToString(tf), copied, fname);
}

string PeriodSuffix(ENUM_TIMEFRAMES tf)
{
    switch(tf)
    {
        case PERIOD_M1:  return "M1";
        case PERIOD_M5:  return "M5";
        case PERIOD_M15: return "M15";
        case PERIOD_M30: return "M30";
        case PERIOD_H1:  return "H1";
        case PERIOD_H4:  return "H4";
        case PERIOD_D1:  return "D1";
        case PERIOD_W1:  return "W1";
        case PERIOD_MN1: return "MN1";
        default:         return "X";
    }
}
