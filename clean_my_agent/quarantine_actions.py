"""Review and act on selected quarantine entries, leaving their siblings intact."""
from . import vault


class QuarantineActions:
    def __init__(self, roots, state):
        self.roots, self.state = roots, state

    def list(self, progress=None):
        rows, errors = [], []
        for key in reversed(vault.batches(self.state)):
            if progress:
                progress('读取暂存清单', detail=key)
            try:
                detail = {}
                _, _, warnings = vault.process(self.state, key, self.roots, 'list', False, detail=detail)
                errors.extend(warnings)
                rows.extend(dict(entry, batch=key, created=detail['created']) for entry in detail['entries'])
            except (OSError, ValueError) as exc:
                errors.append(key+': '+str(exc))
        return dict(items=rows, errors=errors)

    def preview(self, operation, selected, progress):
        if operation not in ('purge', 'restore') or not selected:
            raise ValueError('请先用空格勾选暂存条目')
        groups = {}
        for row in selected:
            group = groups.setdefault(row['batch'], [])
            if any(r['index'] == row['index'] for r in group):
                raise ValueError('选择中有重复条目')
            group.append(row)
        previews = []
        for key, rows in groups.items():
            progress('核对所选暂存条目', detail=key)
            detail = {}
            indices = [r['index'] for r in rows]
            _, _, errors = vault.process(self.state, key, self.roots, operation, False,
                                         detail=detail, indices=indices)
            if errors:
                raise ValueError('；'.join(errors))
            current = {r['index']: r for r in detail['entries']}
            if any(r['created'] != detail['created'] or
                   current.get(r['index']) != {k:r[k] for k in ('index','path','bytes')} for r in rows):
                raise ValueError('暂存条目已变化，请刷新后重新选择')
            previews.append(dict(batch=key, indices=indices, **detail))
        return dict(operation=operation, selected=selected, batches=previews)

    def apply(self, expected, progress):
        # Check every selection before starting; each vault operation checks again under its batch lock.
        fresh = self.preview(expected['operation'], expected['selected'], progress)
        if fresh != expected:
            raise ValueError('暂存清单已变化，未执行')
        count, errors = 0, []
        for batch in fresh['batches']:
            progress('永久删除所选暂存条目' if fresh['operation']=='purge' else '恢复所选暂存条目', detail=batch['batch'])
            try:
                done, _, warnings = vault.process(self.state, batch['batch'], self.roots,
                    fresh['operation'], True, indices=batch['indices'])
                count += done
                errors.extend(warnings)
                if warnings:
                    break
            except (OSError, ValueError) as exc:
                errors.append(str(exc))
                break
        return dict(operation=fresh['operation'], count=count, errors=errors)
