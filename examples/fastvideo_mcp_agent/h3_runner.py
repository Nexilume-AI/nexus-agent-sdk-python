"""Isolated H3 DMD2 job stages, using the native FastVideo trainer."""
import json
from pathlib import Path
import subprocess
import sys
import time
from h3_recipe import H3_REVISION, H3_RUNGS, configuration
from runner import stage


def main():
    import yaml
    request = json.loads(Path('/work/request.json').read_text())
    report = {'base_model':'MiniMaxAI/MiniMax-H3', 'base_revision':H3_REVISION,
        'model_provenance':request['model_provenance'], 'method':'DMD2 student/critic LoRA rank 16',
        'target_timesteps':H3_RUNGS, 'parameter_count_reduced':False,
        'quality_validated':False, 'training_completed':False,
        'note':'Dense SDPA student; not the upstream VSA FastH3 checkpoint. Training completion is not quality certification.'}
    started = time.monotonic()
    try:
        stage('h3_preprocessing_conditioning')
        subprocess.run([sys.executable,'/opt/nexus/h3_preprocess.py'], check=True)
        config = configuration(request, yaml.safe_load(Path('/opt/nexus/wan_dmd2.yaml').read_text()))
        Path('/work/train.yaml').write_text(yaml.safe_dump(config, sort_keys=False))
        launch = ['torchrun','--nnodes=1','--master_addr=127.0.0.1','--master_port=29500','--nproc_per_node=16']
        stage('h3_distilling')
        subprocess.run([*launch,'/opt/nexus/h3_train.py','--config','/work/train.yaml'], check=True)
        report['training_completed'] = True
        report['student_updates_completed'] = request['max_train_steps']//5
        stage('h3_exporting_student')
        subprocess.run([*launch,'/opt/nexus/h3_export.py'], check=True)
        from verify_weights import verify_student
        report['weight_validation'] = verify_student('/work/student','/models/h3')
        report['export_reloaded'] = True
        Path('/work/weights-manifest.json').write_text(json.dumps(report['weight_validation'], indent=2))
        stage('h3_benchmarking_teacher_and_student')
        for role in ('teacher','student'):
            subprocess.run([sys.executable,'/opt/nexus/h3_benchmark.py',role], check=True)
        report['benchmark'] = {role:json.loads((Path('/work/benchmark')/role/'metrics.json').read_text())
                               for role in ('teacher','student')}
        report['paired_audio_video_validated'] = True
        report['measured_speedup'] = (report['benchmark']['teacher']['generation_seconds']/
                                      report['benchmark']['student']['generation_seconds'])
        stage('complete')
    except Exception as exc:
        report['error_type'] = type(exc).__name__
        stage('failed')
        raise
    finally:
        report['wall_seconds'] = round(time.monotonic()-started, 3)
        Path('/work/report.json').write_text(json.dumps(report, indent=2))


if __name__ == '__main__': main()
