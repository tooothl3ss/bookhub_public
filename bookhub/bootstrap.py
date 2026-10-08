"""Attach pre-existing EPUB files before opening the migrated library to readers."""
import json
from .application import initialize


def main():
    store=initialize()
    attached=0
    errors=[]
    for book in store.library()['books']:
        if book['epub_status']=='ready':
            try:
                attached += bool(store.core.attach_kindle(book['key']))
            except Exception as exc:
                errors.append(dict(key=book['key'],error=str(exc)))
    report=dict(attached_epubs=attached,errors=errors)
    (store.root/'bootstrap.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps(dict(attached_epubs=attached,errors=len(errors))))
    if errors:
        raise RuntimeError('Some legacy EPUB files could not be attached; inspect bootstrap.json before activation')


if __name__=='__main__':main()
