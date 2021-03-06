import os
import glob
import click
import pickle
import shutil
import pandas as pd
from pandas import ExcelWriter


@click.command()
@click.option('--redmine_instance', help='Path to pickled Redmine API instance')
@click.option('--issue', help='Path to pickled Redmine issue')
@click.option('--work_dir', help='Path to Redmine issue work directory')
@click.option('--description', help='Path to pickled Redmine description')
def merge_redmine(redmine_instance, issue, work_dir, description):
    # Unpickle Redmine objects
    redmine_instance = pickle.load(open(redmine_instance, 'rb'))
    issue = pickle.load(open(issue, 'rb'))
    description = pickle.load(open(description, 'rb'))

    try:
        # Download the attached excel file.
        # First, get the attachment id - this seems like a kind of hacky way to do this, but I have yet to figure
        # out a better way to do it.
        redmine_instance.issue.update(resource_id=issue.id,
                                      notes='Started merging...')
        attachment = redmine_instance.issue.get(issue.id, include='attachments')
        attachment_id = 0
        for item in attachment.attachments:
            attachment_id = item.id

        # Now download, if attachment id is not 0, which indicates that we didn't find anything attached to the issue.
        if attachment_id != 0:
            attachment = redmine_instance.attachment.get(attachment_id)
            attachment.download(savepath=work_dir, filename='merge.xlsx')
        else:
            redmine_instance.issue.update(resource_id=issue.id,
                                          notes='ERROR: Did not find any attached files. Please create a new issue with '
                                                'the merge excel file attached and try again.',
                                          status_id=4)
            return

        redmine_instance.issue.update(resource_id=issue.id,
                                      notes='Downloaded excel file...')
        # Now use convert_excel_file to make compatible with merger.py
        convert_excel_file(os.path.join(work_dir, 'merge.xlsx'), os.path.join(work_dir, 'Merge.xlsx'))

        # Make a SEQID list of files we'll need to extract.
        seqid_list = generate_seqid_list(os.path.join(work_dir, 'Merge.xlsx'))

        # Write SEQID list to file and extract FASTQ files to be merged.
        with open(os.path.join(work_dir, 'list.txt'), 'w') as f:
            for seqid in seqid_list:
                f.write(seqid + '\n')

        cmd = 'python2 /mnt/nas/MiSeq_Backup/file_linker.py {} {}'.format(os.path.join(work_dir, 'list.txt'),
                                                                          work_dir)
        os.system(cmd)

        # Run the merger script.
        cmd = 'python /mnt/nas/Redmine/OLCRedmineAutomator/automators/merger.py -f {} -d ";" {}'.format(os.path.join(work_dir, 'Merge.xlsx'),
                                                        work_dir)
        os.system(cmd)

        # Make a folder to put all the merged FASTQs in biorequest folder. and put the merged FASTQs there.
        os.makedirs(os.path.join(work_dir, 'merged_' + str(issue.id)))
        cmd = 'mv {merged_files} {merged_folder}'.format(merged_files=os.path.join(work_dir, '*MER*/*.fastq.gz'),
                                                         merged_folder=os.path.join(work_dir, 'merged_' + str(issue.id)))
        os.system(cmd)

        if len(glob.glob(os.path.join(work_dir, 'merged_' + str(issue.id), '*fastq.gz'))) == 0:
            redmine_instance.issue.update(resource_id=issue.id,
                                          notes='ERROR: Something went wrong, no merged FASTQ files were created.',
                                          status_id=4)
            return
        # Now copy those merged FASTQS to merge backup and the hdfs folder so they can be assembled.
        cmd = 'cp {merged_files} /mnt/nas/merge_Backup'.format(merged_files=os.path.join(work_dir, 'merged_' + str(issue.id),
                                                                                         '*.fastq.gz'))
        os.system(cmd)

        cmd = 'cp -r {merged_folder} /hdfs'.format(merged_folder=os.path.join(work_dir, 'merged_' + str(issue.id)))
        os.system(cmd)

        redmine_instance.issue.update(resource_id=issue.id,
                                      notes='Merged FASTQ files created, beginning assembly of merged files.')
        # With files copied over to the HDFS, start the assembly process.
        os.system('docker rm -f spadespipeline')
        # Run docker image.
        cmd = 'docker run -i -u $(id -u) -v /mnt/nas/Adam/spadespipeline/OLCspades/:/spadesfiles ' \
              '-v /mnt/nas/Adam/assemblypipeline/:/pipelinefiles -v  {}:/sequences ' \
              '--name spadespipeline pipeline:0.1.5 OLCspades.py ' \
              '/sequences -r /pipelinefiles'.format(os.path.join('/hdfs', 'merged_' + str(issue.id)))
        os.system(cmd)
        # Remove the container.
        os.system('docker rm -f spadespipeline')

        # Move results to merge_WGSspades, and upload the results folder to redmine.
        cmd = 'mv {hdfs_folder} {merge_WGSspades}'.format(hdfs_folder=os.path.join('/hdfs', 'merged_' + str(issue.id)),
                                                          merge_WGSspades=os.path.join('/mnt/nas/merge_WGSspades',
                                                          'merged_' + str(issue.id) + '_Assembled'))
        os.system(cmd)
        shutil.make_archive(os.path.join(work_dir, 'reports'), 'zip', os.path.join('/mnt/nas/merge_WGSspades', 'merged_' + str(issue.id) + '_Assembled', 'reports'))
        output_list = list()
        output_dict = dict()
        output_dict['path'] = os.path.join(work_dir, 'reports.zip')
        output_dict['filename'] = 'merged_' + str(issue.id) + '_reports.zip'
        output_list.append(output_dict)
        redmine_instance.issue.update(resource_id=issue.id, uploads=output_list, status_id=4,
                                      notes='Merge Process Complete! Reports attached.')
    except Exception as e:
        redmine_instance.issue.update(resource_id=issue.id,
                                      notes='Something went wrong! Send this error traceback to your friendly '
                                            'neighborhood bioinformatician: {}'.format(e))


def convert_excel_file(infile, outfile):
    df = pd.read_excel(infile)
    to_keep = ['SEQID', 'OtherName']
    for column in df:
        if column not in to_keep:
            df = df.drop(column, axis=1)
    df = df.rename(columns={'SEQID': 'Name', 'OtherName': 'Merge'})
    writer = ExcelWriter(outfile)
    df.to_excel(writer, 'Sheet1', index=False)
    writer.save()


def generate_seqid_list(mergefile):
    df = pd.read_excel(mergefile)
    seqid_list = list()
    seqids = list(df['Merge'])
    for row in seqids:
        for item in row.split(';'):
            seqid_list.append(item)
    return seqid_list


if __name__ == '__main__':
    merge_redmine()