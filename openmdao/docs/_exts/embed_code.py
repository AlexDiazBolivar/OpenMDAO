
import unittest
from docutils import nodes
from sphinx.util.compat import Directive
from sphinx.errors import SphinxError

import traceback
import fnmatch
import inspect
import os

from docutils.parsers.rst.directives import unchanged, images

from openmdao.docs._utils.docutil import get_source_code, remove_docstrings, \
    remove_initial_empty_lines, replace_asserts_with_prints, \
    strip_header, dedent, insert_output_start_stop_indicators, run_code, process_output, \
    get_skip_output_node, get_interleaved_io_nodes, get_output_block_node, \
    split_source_into_input_blocks, extract_output_blocks, clean_up_empty_output_blocks


class skipped_or_failed_node(nodes.Element):
    pass


_plot_count = 0


class EmbedCodeDirective(Directive):
    """EmbedCodeDirective is a custom directive to allow blocks of
     python code to be shown in feature docs.  An example usage would look like this:

    .. embed-code::
        openmdao.test.whatever.method

    What the above will do is replace the directive and its args with the block of code
    for the class or method desired.

    By default, docstrings will be kept in the embedded code. There is an option
    to the directive to strip the docstrings:

    .. embed-code::
        openmdao.test.whatever.method
        :strip-docstrings:
    """

    # must have at least one directive for this to work
    required_arguments = 1
    has_content = True

    option_spec = {
        'strip-docstrings': unchanged,
        'layout': unchanged,
        'scale': unchanged,
        'align': unchanged,
    }

    def run(self):
        global _plot_count

        allowed_layouts = set(['code', 'output', 'interleave', 'plot'])

        path = self.arguments[0]
        is_script = path.endswith('.py')

        try:
            source, indent, module, class_ = get_source_code(path)
        except Exception as err:
            raise SphinxError(str(err))

        is_test = class_ is not None and issubclass(class_, unittest.TestCase)
        shows_plot = '.show(' in source

        if 'layout' in self.options:
            layout = [s.strip() for s in self.options['layout'].split(',')]
        else:
            layout = ['code']

        if len(layout) > len(set(layout)):
            raise SphinxError("No duplicate layout entries allowed.")
        bad = [n for n in layout if n not in allowed_layouts]
        if bad:
            raise SphinxError("The following layout options are invalid: %s" % bad)
        if 'interleave' in layout and ('code' in layout or 'output' in layout):
            raise SphinxError("The interleave option is mutually exclusive to the code "
                              "and print options.")

        remove_docstring = 'strip-docstrings' in self.options
        do_run = 'output' in layout or 'interleave' in layout or 'plot' in layout

        if 'plot' in layout:
            plot_dir = os.path.dirname(path)
            plot_fname = 'doc_plot_%d.png' % _plot_count
            _plot_count += 1
            plot_file_abs = os.path.join(os.path.abspath(plot_dir), plot_fname)
            if os.path.isfile(plot_file_abs):
                # remove any existing plot file
                os.remove(plot_file_abs)

        # Modify the source prior to running
        if remove_docstring:
            source = remove_docstrings(source)

        if is_test:
            source = replace_asserts_with_prints(dedent(strip_header(source)))
            source = remove_initial_empty_lines(source)

            class_name = class_.__name__
            method_name = path.rsplit('.', 1)[1]

            # make 'self' available to test code (as an instance of the test case)
            self_code = "from %s import %s\nself = %s('%s')\n" % \
                        (module.__name__, class_name, class_name, method_name)

            # get setUp and tearDown but don't duplicate if it is the method being tested
            setup_code = '' if method_name == 'setUp' else dedent(strip_header(remove_docstrings(
                inspect.getsource(getattr(class_, 'setUp')))))
            if setup_code.strip() == 'pass':
                setup_code = ''

            teardown_code = '' if method_name == 'tearDown' else dedent(strip_header(
                remove_docstrings(inspect.getsource(getattr(class_, 'tearDown')))))
            if teardown_code.strip() == 'pass':
                teardown_code = ''

            code_to_run = '\n'.join([self_code, setup_code, source, teardown_code])
            code_to_display = '\n'.join([setup_code, source, teardown_code])
        else:
            if indent > 0:
                source = dedent(source)
            code_to_run = code_to_display = source[:]

        if 'interleave' in layout:
            code_to_run = insert_output_start_stop_indicators(code_to_run)

        # Run the source (if necessary)
        skipped = failed = False
        if do_run:
            if shows_plot:
                # insert lines to generate the plot file
                parts = ['import matplotlib', 'matplotlib.use("Agg")', code_to_run]
                if 'plot' in layout:
                    parts.append('matplotlib.pyplot.savefig("%s")' % plot_fname)
                skipped, failed, run_outputs = \
                    run_code('\n'.join(parts), path, module=module, cls=class_)
            else:
                skipped, failed, run_outputs = \
                    run_code(code_to_run, path, module=module, cls=class_)

        if failed:
            io_nodes = [get_skip_output_node(run_outputs, "failed")]
        elif skipped:
            io_nodes = [get_skip_output_node(run_outputs, "skipped")]
        else:
            if 'output' in layout:
                output_blocks = run_outputs if isinstance(run_outputs, list) else [run_outputs]
            elif 'interleave' in layout:
                input_blocks = split_source_into_input_blocks(code_to_run)
                output_blocks = extract_output_blocks(run_outputs)

                # the last input block may not produce any output
                if len(output_blocks) == len(input_blocks) - 1:
                    output_blocks.append('')

                # Need to deal with the cases when there is no output for a given input block
                # Merge an input block with the previous block and throw away the output block
                input_blocks, output_blocks = clean_up_empty_output_blocks(input_blocks,
                                                                           output_blocks)

            if 'plot' in layout:
                if not os.path.isfile(plot_file_abs):
                    raise SphinxError("Can't find plot file '%s'" % plot_file_abs)

                directive_dir = os.path.relpath(os.getcwd(),
                                                os.path.dirname(self.state.document.settings._source))
                plot_file = os.path.join(directive_dir, plot_dir, plot_fname)
                # create plot node
                fig = images.Figure(self.name, [plot_file], self.options, self.content, self.lineno,
                                    self.content_offset, self.block_text, self.state,
                                    self.state_machine)
                plot_nodes = fig.run()

        # create a list of document nodes to return based on layout
        doc_nodes = []
        skip_fail_shown = False
        for opt in layout:
            if opt == 'code':
                # we want the body of code to be formatted and code highlighted
                body = nodes.literal_block(source, source)
                body['language'] = 'python'
                doc_nodes.append(body)
            elif skipped or failed:
                if not skip_fail_shown:
                    doc_nodes.extend(io_nodes)
                    skip_fail_shown = True
            else:
                if opt == 'interleave':
                    doc_nodes.extend(get_interleaved_io_nodes(input_blocks, output_blocks))
                elif opt == 'output':
                    doc_nodes.append(get_output_block_node(output_blocks))
                else:  # plot
                    doc_nodes.extend(plot_nodes)

        return doc_nodes


def setup(app):
    """add custom directive into Sphinx so that it is found during document parsing"""
    app.add_directive('embed-code', EmbedCodeDirective)
