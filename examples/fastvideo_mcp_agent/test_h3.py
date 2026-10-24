"""Family isolation, honest readiness and model-specific request contracts."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from h3_recipe import H3_ID, H3_REVISION, configuration, status
from model_registry import BASE_ID, Registry
from service import CATALOG, Service, validated


class H3Tests(unittest.TestCase):
    def test_family_dimensions_and_defaults(self):
        request = {'prompts':['a','b','c','d']}
        self.assertEqual(validated(request)['num_frames'], 5)
        self.assertEqual(validated(request,H3_ID)['num_frames'],124)
        for family, overrides in ((BASE_ID,{'num_frames':124}),
                                  (H3_ID,{'num_frames':5}),
                                  (H3_ID,{'width':80}),
                                  (H3_ID,{'max_train_steps':6})):
            with self.assertRaises(ValueError): validated({**request,**overrides},family)

    def test_weights_alone_never_enable_training(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name)
            self.assertFalse(status(root)['ready'])
            (root/'models/minimax-h3').mkdir(parents=True)
            (root/'models/minimax-h3/WEIGHTS_VERIFIED.json').write_text('{')
            self.assertFalse(status(root)['ready'])
            (root/'models/minimax-h3/WEIGHTS_VERIFIED.json').write_text('{}')
            self.assertFalse(status(root)['ready'])
            evidence={'revision':H3_REVISION, 'real_weights':True, 'training_completed':True,
                      'export_reloaded':True,'paired_audio_video_validated':False}
            (root/'h3-acceptance.json').write_text(json.dumps(evidence))
            self.assertFalse(status(root)['ready'])

    def test_h3_configuration_does_not_mutate_wan(self):
        import yaml
        base=yaml.safe_load(Path(__file__).with_name('wan_dmd2.yaml').read_text())
        original=json.dumps(base,sort_keys=True)
        req=validated({'prompts':['a','b','c','d']},H3_ID)
        config=configuration(req,base)
        self.assertEqual(original,json.dumps(base,sort_keys=True))
        self.assertEqual(config['training']['data']['num_latent_t'],37)
        self.assertEqual(config['models']['student']['lora']['rank'],16)
        self.assertNotIn('lora',config['models']['teacher'])
        self.assertEqual(config['method']['real_score_guidance_scale'],0)
        self.assertNotIn('flow_shift',config['pipeline'])

    def test_mcp_import_schema_has_explicit_family(self):
        schema=next(t for t in CATALOG if t['name']=='begin_model_import')['inputSchema']
        self.assertEqual(schema['properties']['family']['enum'],[BASE_ID,H3_ID])
        self.assertFalse(schema['additionalProperties'])

    def test_large_import_issues_bounded_link_batches(self):
        with tempfile.TemporaryDirectory() as name, patch('model_registry.h3_status',return_value={'ready':True}):
            root=Path(name)
            registry=Registry({'folder':root,'closed':False},root/'wan','test-image',root)
            files=[{'name':f'shard-{i}.safetensors','size_bytes':16,'sha256':'a'*64} for i in range(30)]
            with patch.object(registry,'upload',return_value={'url':'test'}) as upload:
                result=registry.begin({'name':'large H3','family':H3_ID,'weight_files':files},None)
                self.assertEqual(upload.call_count,8)
                self.assertEqual(len(result['remaining_upload_files']),22)
            row=registry.rows[result['id']]
            first=registry.folder(row)/'uploads'/files[0]['name']
            first.parent.mkdir(parents=True);first.write_bytes(b'x'*16)
            registry.bridges=Mock()
            registry.bridges.create.return_value={'url':'test'}
            registry.upload(row,files[8]['name'],None)
            registry.bridges.revoke_path.assert_any_call(first)

    def test_operator_probe_is_opt_in_and_queued_once(self):
        with tempfile.TemporaryDirectory() as name, patch('service.ROOT',Path(name)):
            root=Path(name); service=Service.__new__(Service);service.pending=[]
            service.enqueue_h3_acceptance()
            self.assertEqual(service.pending,[])
            (root/'h3-provision-enabled.json').write_text(json.dumps({'revision':H3_REVISION}))
            service.enqueue_h3_acceptance()
            self.assertEqual(service.pending,[])
            (root/'models/minimax-h3').mkdir(parents=True)
            (root/'models/minimax-h3/WEIGHTS_VERIFIED.json').write_text(json.dumps({
                'revision':H3_REVISION,'repo':'MiniMaxAI/MiniMax-H3'}))
            service.enqueue_h3_acceptance();service.enqueue_h3_acceptance()
            self.assertEqual(len(service.pending),1)
            self.assertTrue(service.pending[0]['operator_acceptance'])
            self.assertEqual(service.pending[0]['family'],H3_ID)

    def test_failed_or_incomplete_probe_cannot_enable_h3(self):
        with tempfile.TemporaryDirectory() as name, patch('service.ROOT',Path(name)):
            root=Path(name);service=Service.__new__(Service)
            job={'state':'failed','folder':root,'id':'test'}
            service.finish_h3_acceptance(job)
            self.assertFalse((root/'h3-acceptance.json').exists())
            job['state']='succeeded'
            report={'base_revision':H3_REVISION,'training_completed':True,'export_reloaded':True,
                    'paired_audio_video_validated':False}
            (root/'report.json').write_text(json.dumps(report))
            service.finish_h3_acceptance(job)
            self.assertFalse((root/'h3-acceptance.json').exists())


if __name__=='__main__': unittest.main()
