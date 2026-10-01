"""Read-only release preflight. Missing protection is BLOCKED, never waived."""
import argparse
import json
import subprocess


def validate(protection):
    checks=protection.get('required_status_checks') or {}
    names=set(checks.get('contexts') or []) | {c['context'] for c in checks.get('checks',[])}
    reviews=protection.get('required_pull_request_reviews') or {}
    errors=[]
    if 'Required regression gate' not in names or not checks.get('strict'):
        errors.append('up-to-date Required regression gate is not mandatory')
    if reviews.get('required_approving_review_count',0) < 1 or not reviews.get('dismiss_stale_reviews'):
        errors.append('fresh independent approval is not mandatory')
    if not reviews.get('require_code_owner_reviews') or not reviews.get('require_last_push_approval'):
        errors.append('gate ownership / latest push approval is not mandatory')
    if not protection.get('enforce_admins',{}).get('enabled'):
        errors.append('administrators may bypass the merge gate')
    for name in ('allow_force_pushes','allow_deletions'):
        if protection.get(name,{}).get('enabled'):
            errors.append(name+' must be disabled')
    if errors:
        raise ValueError('; '.join(errors))


if __name__ == '__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--repo',default='OwenYWT/CAD-Agent')
    args=parser.parse_args()
    response=subprocess.run(['gh','api',f'repos/{args.repo}/branches/main/protection'],capture_output=True,text=True)
    if response.returncode:
        raise SystemExit('BLOCKED: cannot verify main protection: '+response.stderr.strip())
    validate(json.loads(response.stdout))
    print('Main protection policy verified; a failing-PR merge refusal must also be recorded before release.')
