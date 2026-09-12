"""Strict member-name inventory. Incomplete enumeration is not absence."""


def catalog(item, *, max_items=4096, max_depth=16):
    entries=[]
    count=0
    def walk(parent,prefix,depth):
        nonlocal count
        if depth>max_depth: raise ValueError('Member folder depth budget exceeded')
        for child in list(parent):
            count+=1
            if count>max_items: raise ValueError('Member inventory budget exceeded')
            name=str(child.Name)
            kind=int(child.ItemType)
            if not name: raise ValueError('Member name unavailable')
            path=prefix+[name]
            if kind==601:
                walk(child,path,depth+1)
            elif kind in {608,609,610,611,612,613,614,616,654,655}:
                entries.append({'name':name,'relative_path':'.'.join(path),'itemType':kind})
            else:
                raise ValueError('Unknown child kind prevents proof of member absence')
    try:
        walk(item,[],0)
        return {'complete':True,'entries':entries}
    except Exception as exc:
        return {'complete':False,'entries':entries,'reason':str(exc)[:200]}
