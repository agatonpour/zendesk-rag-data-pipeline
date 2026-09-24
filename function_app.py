import logging
import azure.functions as func

from sync.service import run_sync

app = func.FunctionApp()


@app.schedule(schedule="0 0 2 * * *", arg_name="mytimer", run_on_startup=False, use_monitor=True)
def zendesk_sharepoint_sync(mytimer: func.TimerRequest) -> None:
    logging.info("Zendesk -> SharePoint nightly sync started.")
    try:
        run_sync()
    except Exception:
        logging.exception("Zendesk -> SharePoint nightly sync failed.")
        raise
    logging.info("Zendesk -> SharePoint nightly sync finished.")
